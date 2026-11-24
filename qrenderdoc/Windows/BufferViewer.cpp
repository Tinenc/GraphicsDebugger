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

#include "BufferViewer.h"
#include <float.h>
#include <limits.h>
#include <QApplication>
#include <QBuffer>
#include <QButtonGroup>
#include <QCheckBox>
#include <QClipboard>
#include <QCryptographicHash>
#include <QDataStream>
#include <QDateTime>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDoubleSpinBox>
#include <QDoubleValidator>
#include <QElapsedTimer>
#include <QFileInfo>
#include <QFileSystemWatcher>
#include <QFontDatabase>
#include <QItemSelection>
#include <QJsonDocument>
#include <QJsonObject>
#include <QKeyEvent>
#include <QLabel>
#include <QLineEdit>
#include <QMenu>
#include <QMouseEvent>
#include <QMutexLocker>
#include <QPainter>
#include <QPointer>
#include <QPushButton>
#include <QRegularExpression>
#include <QRubberBand>
#include <QScopedValueRollback>
#include <QScrollBar>
#include <QSet>
#include <QShortcut>
#include <QSignalBlocker>
#include <QSlider>
#include <QSplitter>
#include <QStackedWidget>
#include <QTimer>
#include <QToolButton>
#include <QToolTip>
#include <QWidgetAction>
#include <QtMath>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <functional>
#include <limits>
#include "AI/AIVisualReply.h"
#include "Code/QRDUtils.h"
#include "Code/ReplayLab/ReplayLabAIResourceAttachment.h"
#include "Code/Resources.h"
#include "Widgets/CollapseGroupBox.h"
#include "Widgets/ComputeDebugSelector.h"
#include "Widgets/Extended/RDLabel.h"
#include "Widgets/Extended/RDSplitter.h"
#include "Windows/Dialogs/AxisMappingDialog.h"
#include "Windows/Dialogs/CameraControlsDialog.h"
#include "MeshUVView.h"
#include "ui_BufferViewer.h"

struct FixedVarTag
{
  FixedVarTag() = default;
  FixedVarTag(uint32_t size) : valid(true), padding(true), byteSize(size) {}
  FixedVarTag(rdcstr varName, uint32_t offset)
      : valid(true), padding(false), name(varName), byteOffset(offset)
  {
  }
  bool valid = false;
  bool padding = false;
  bool matrix = false;
  bool rowmajor = false;
  // This is only a UI eligibility hint. ReplayLab re-reads the exact reflection and issues an
  // allow-listed leaf ID before it accepts a material edit.
  bool replayLabMaterialLeaf = false;
  bool replayLabMatrixRow = false;
  VarType replayLabBaseType = VarType::Unknown;
  uint8_t replayLabComponents = 0;
  rdcstr name;
  // ReplayLab alias view: the reflected path this row stands for, whether it is a leaf whose
  // byte offset identifies it, and the display alias (empty = original name shown).
  QString path;
  bool leaf = false;
  QString alias;
  QString aliasSource;
  ShaderVariable replayLabDisplayValue;
  QString replayLabSemanticTooltip;
  QStringList replayLabOriginalPaths;
  // These are individually proven physical 32-bit lanes, in display order. A semantic row
  // remains ineligible for the extension leaf API; direct editing uses this separate mapping.
  QList<quint64> replayLabEditOffsets;
  union
  {
    uint32_t byteOffset;
    uint32_t byteSize;
  };
};

Q_DECLARE_METATYPE(FixedVarTag);

static void AppendCBufferChildren(RDTreeWidgetItem *target, RDTreeWidgetItem *source);

static bool CanAttachCBufferParameter(const FixedVarTag &tag)
{
  // A semantic display row is useful reference context without being an extension leaf identity.
  return !tag.padding &&
         (tag.valid || tag.replayLabMatrixRow || (!tag.replayLabSemanticTooltip.isEmpty() &&
                        !tag.replayLabEditOffsets.isEmpty()));
}

static bool CBufferSearchMatches(const RDTreeWidgetItem *item, const QStringList &terms)
{
  const FixedVarTag tag = item->tag().value<FixedVarTag>();
  QStringList searchable;
  for(int column = 0; column < item->dataCount(); column++)
    searchable.push_back(item->text(column));
  searchable << tag.path << QString::fromUtf8(tag.name.c_str()) << tag.alias << tag.aliasSource;
  searchable << tag.replayLabOriginalPaths;
  if(tag.valid && !tag.padding)
    searchable << QString::number(tag.byteOffset)
               << QStringLiteral("0x%1").arg(tag.byteOffset, 0, 16);
  for(quint64 offset : tag.replayLabEditOffsets)
    searchable << QString::number(offset) << QStringLiteral("0x%1").arg(offset, 0, 16);
  const QString text = searchable.join(QLatin1Char(' '));
  for(const QString &term : terms)
    if(!text.contains(term, Qt::CaseInsensitive))
      return false;
  return true;
}

static bool CBufferSearchVisibleItems(const RDTreeWidgetItem *item, const QStringList &terms,
                                      QSet<const RDTreeWidgetItem *> &visible,
                                      bool parentMatches = false)
{
  const bool ownMatch = parentMatches || CBufferSearchMatches(item, terms);
  bool childMatches = false;
  for(int row = 0; row < item->childCount(); row++)
    childMatches =
        CBufferSearchVisibleItems(item->child(row), terms, visible, ownMatch) || childMatches;
  if(ownMatch || childMatches)
    visible.insert(item);
  return ownMatch || childMatches;
}

static QString CBufferAliasFileStamp(const QString &path)
{
  const QFileInfo file(path);
  if(!file.exists())
    return QString();
  return QString::number(file.size()) + QLatin1Char(':') +
         QString::number(file.lastModified().toMSecsSinceEpoch());
}

static QString ReplayLabShaderScopeStage(ShaderStage stage)
{
  switch(stage)
  {
    // i18n-ignore: stable ShaderScope stage protocol identifier.
    case ShaderStage::Vertex: return lit("vertex");
    // i18n-ignore: stable ShaderScope stage protocol identifier.
    case ShaderStage::Hull: return lit("hull");
    // i18n-ignore: stable ShaderScope stage protocol identifier.
    case ShaderStage::Domain: return lit("domain");
    // i18n-ignore: stable ShaderScope stage protocol identifier.
    case ShaderStage::Geometry: return lit("geometry");
    // i18n-ignore: stable ShaderScope stage protocol identifier.
    case ShaderStage::Pixel: return lit("fragment");
    // i18n-ignore: stable ShaderScope stage protocol identifier.
    case ShaderStage::Compute: return lit("compute");
    // i18n-ignore: stable ShaderScope stage protocol identifier.
    case ShaderStage::Task: return lit("task");
    // i18n-ignore: stable ShaderScope stage protocol identifier.
    case ShaderStage::Mesh: return lit("mesh");
    default: break;
  }
  return QString();
}

static QString ReplayLabCurrentShaderRawSha256(const ShaderReflection *reflection)
{
  if(reflection == NULL || reflection->rawBytes.empty() ||
     reflection->rawBytes.size() > size_t(INT_MAX))
    return QString();
  const QByteArray raw(reinterpret_cast<const char *>(reflection->rawBytes.data()),
                       int(reflection->rawBytes.size()));
  return QString::fromLatin1(QCryptographicHash::hash(raw, QCryptographicHash::Sha256).toHex());
}

static void ReplayLabLoadCBufferAliases(ICaptureContext &ctx, ReplayLabCBufferAliases &aliases,
                                        const ConstantBlock *block,
                                        const ShaderReflection *reflection, ShaderStage stage,
                                        const CBufferDebugDisplaySource *knownSource = NULL)
{
  if(block == NULL)
  {
    aliases.Load(NULL);
    return;
  }
  const ResourceId shaderResource = reflection != NULL ? reflection->resourceId : ResourceId();
  QString shaderResourceId;
  if(shaderResource != ResourceId())
  {
    // ResourceId's display form is "ResourceId::<decimal>". The Host stores
    // only the decimal payload as a JSON string so no 64-bit precision is lost.
    const QString encoded = ToQStr(shaderResource);
    const QString prefix = lit("ResourceId::");    // i18n-ignore: stable ResourceId serialization.
    if(encoded.startsWith(prefix))
      shaderResourceId = encoded.mid(prefix.size());
  }
  const bool originalShaderIsReplaced =
      shaderResource != ResourceId() && ctx.IsResourceReplaced(shaderResource);
  const QString activeSha = ReplayLabCurrentShaderRawSha256(reflection);
  const CBufferDebugDisplaySource source = knownSource ? *knownSource :
      ctx.GetCBufferDebugDisplaySource(shaderResource, stage, activeSha, *block);
  const bool temporary = !source.sourceRawSha256.empty();
  const ConstantBlock *namesBlock = temporary ? &source.sourceBlock : block;
  aliases.Load(namesBlock, temporary ? QString(source.sourceRawSha256) : activeSha,
               ReplayLabShaderScopeStage(stage), namesBlock->fixedBindNumber,
               namesBlock->fixedBindSetOrSpace, shaderResourceId, originalShaderIsReplaced);
  if(temporary)
  {
    aliases.DiscardUsageReview();
    aliases.UsePhysicalOffsetAliases();
    if(ReplayLabCBufferAliases::LayoutHash(*block) != aliases.LayoutSha256())
    {
      ReplayLabCBufferAliases current;
      current.Load(block, activeSha, ReplayLabShaderScopeStage(stage), block->fixedBindNumber,
                   block->fixedBindSetOrSpace, shaderResourceId, originalShaderIsReplaced);
      aliases.PreferUserAliases(current);
    }
  }
}

static bool IsReplayLabMaterialLeaf(const ShaderConstant &constant, const ShaderVariable &variable)
{
  if(constant.bitFieldOffset != 0 || constant.bitFieldSize != 0 || !variable.members.empty())
    return false;

  if(variable.rows != 1 || variable.columns < 1 || variable.columns > 4)
    return false;

  return constant.type.baseType == VarType::Float || constant.type.baseType == VarType::SInt ||
         constant.type.baseType == VarType::UInt;
}

static FixedVarTag ReplayLabFixedVarTag(const ShaderConstant &constant,
                                        const ShaderVariable &variable, uint32_t byteOffset)
{
  FixedVarTag tag(variable.name, byteOffset);
  tag.replayLabMaterialLeaf = IsReplayLabMaterialLeaf(constant, variable);
  tag.replayLabBaseType = constant.type.baseType;
  tag.replayLabComponents = variable.columns;
  if(tag.replayLabMaterialLeaf)
  {
    tag.replayLabDisplayValue = variable;
    for(int col = 0; col < variable.columns; col++)
      tag.replayLabEditOffsets.push_back(uint64_t(byteOffset) + col * 4);
  }
  return tag;
}

static FixedVarTag ReplayLabMatrixRowTag(const ShaderConstant &constant,
                                         const ShaderVariable &variable,
                                         const FixedVarTag &parent, uint32_t row)
{
  FixedVarTag tag;
  const ShaderConstantType &type = constant.type;
  const uint32_t minor = type.RowMajor() ? type.columns : type.rows;
  if(constant.bitFieldOffset || constant.bitFieldSize || !variable.members.empty() ||
     type.rows != variable.rows || type.columns != variable.columns || type.rows < 2 ||
     type.rows > 4 || type.columns < 1 || type.columns > 4 || row >= type.rows ||
     (type.baseType != VarType::Float && type.baseType != VarType::SInt &&
      type.baseType != VarType::UInt) || type.matrixByteStride < minor * 4 ||
     type.matrixByteStride % 4 != 0 || (variable.flags & ShaderVariableFlags::Truncated))
    return tag;

  // Decoded ShaderVariable values are logical row-major; their physical lanes
  // may be strided columns. Keep this display row out of the extension leaf API.
  tag.replayLabMatrixRow = true;
  tag.path = parent.path + QStringLiteral(".row%1").arg(row);
  tag.name = tag.path;
  tag.replayLabBaseType = type.baseType;
  tag.replayLabComponents = type.columns;
  tag.replayLabDisplayValue = variable;
  tag.replayLabDisplayValue.rows = 1;
  for(uint32_t column = 0; column < type.columns; column++)
  {
    const uint64_t offset = uint64_t(parent.byteOffset) +
                           (type.RowMajor() ? uint64_t(row) * type.matrixByteStride + column * 4
                                            : uint64_t(column) * type.matrixByteStride + row * 4);
    if(offset > UINT32_MAX - 3)
      return FixedVarTag();
    tag.replayLabEditOffsets.push_back(offset);
    tag.replayLabDisplayValue.value.u32v[column] = variable.value.u32v[row * type.columns + column];
  }
  tag.byteOffset = uint32_t(tag.replayLabEditOffsets.front());
  return tag;
}

static QString CBufferValueText(const QLocale &locale, double value, bool integer)
{
  return locale.toString(value, 'g', integer ? 10 : 9);
}

class CBufferValueSpinBox : public QDoubleSpinBox
{
public:
  CBufferValueSpinBox(QWidget *parent) : QDoubleSpinBox(parent) {}
  QString textFromValue(double value) const override
  {
    // float32 round-trip precision, without hundreds of fixed decimals for small values.
    return CBufferValueText(locale(), value, decimals() == 0);
  }
  double valueFromText(const QString &text) const override
  {
    bool valid = false;
    const double parsed = locale().toDouble(text, &valid);
    return valid ? parsed : value();
  }
  QValidator::State validate(QString &text, int &position) const override
  {
    QDoubleValidator validator(minimum(), maximum(), decimals());
    validator.setLocale(locale());
    validator.setNotation(QDoubleValidator::ScientificNotation);
    return validator.validate(text, position);
  }
};

// Keep HSV interaction independent from the physical CBuffer values. The caller retains HDR
// intensity and alpha, and owns the live-preview transaction and its rollback on Cancel.
static QPointF CBufferSVAtPoint(const QRectF &area, const QPointF &point)
{
  return QPointF(qBound(0.0, (point.x() - area.left()) / qMax(1.0, area.width()), 1.0),
                 qBound(0.0, 1.0 - (point.y() - area.top()) / qMax(1.0, area.height()), 1.0));
}

class CBufferSaturationValue : public QWidget
{
public:
  explicit CBufferSaturationValue(QWidget *parent) : QWidget(parent)
  {
    setObjectName(QStringLiteral("cbufferColorSV"));
    setFocusPolicy(Qt::StrongFocus);
    setMinimumSize(220, 220);
    setAccessibleName(QCoreApplication::translate("BufferViewer", "Saturation and brightness"));
    setAccessibleDescription(QCoreApplication::translate(
        "BufferViewer",
        "Left/right adjusts saturation; up/down adjusts brightness. Hold Shift for larger steps."));
    setToolTip(accessibleDescription());
  }

  std::function<void(qreal, qreal)> changed;
  void setHSV(qreal hue, qreal saturation, qreal value)
  {
    m_Hue = hue;
    m_Saturation = saturation;
    m_Value = value;
    update();
  }

protected:
  void paintEvent(QPaintEvent *) override
  {
    QPainter painter(this);
    const QRectF area = colorArea();
    QLinearGradient saturation(area.topLeft(), area.topRight());
    saturation.setColorAt(0.0, Qt::white);
    saturation.setColorAt(1.0, QColor::fromHsvF(m_Hue, 1.0, 1.0));
    painter.fillRect(area, saturation);
    QLinearGradient value(area.topLeft(), area.bottomLeft());
    value.setColorAt(0.0, QColor(0, 0, 0, 0));
    value.setColorAt(1.0, Qt::black);
    painter.fillRect(area, value);
    painter.setPen(QPen(palette().color(hasFocus() ? QPalette::Highlight : QPalette::Mid),
                        hasFocus() ? 2 : 1));
    painter.drawRect(area);
    const QPointF marker(area.left() + m_Saturation * area.width(),
                         area.top() + (1.0 - m_Value) * area.height());
    painter.setRenderHint(QPainter::Antialiasing);
    painter.setPen(QPen(Qt::black, 3));
    painter.drawEllipse(marker, 5, 5);
    painter.setPen(QPen(Qt::white, 1));
    painter.drawEllipse(marker, 5, 5);
  }
  void mousePressEvent(QMouseEvent *event) override
  {
    if(event->button() != Qt::LeftButton)
      return;
    setFocus(Qt::MouseFocusReason);
    updateFromPoint(event->localPos());
  }
  void mouseMoveEvent(QMouseEvent *event) override
  {
    if(event->buttons() & Qt::LeftButton)
      updateFromPoint(event->localPos());
  }
  void keyPressEvent(QKeyEvent *event) override
  {
    const qreal step = (event->modifiers() & Qt::ShiftModifier) ? 0.1 : 0.01;
    qreal saturation = m_Saturation, value = m_Value;
    switch(event->key())
    {
      case Qt::Key_Left: saturation -= step; break;
      case Qt::Key_Right: saturation += step; break;
      case Qt::Key_Up: value += step; break;
      case Qt::Key_Down: value -= step; break;
      default: QWidget::keyPressEvent(event); return;
    }
    if(changed)
      changed(qBound(0.0, saturation, 1.0), qBound(0.0, value, 1.0));
    event->accept();
  }
  void focusInEvent(QFocusEvent *event) override
  {
    QWidget::focusInEvent(event);
    update();
  }
  void focusOutEvent(QFocusEvent *event) override
  {
    QWidget::focusOutEvent(event);
    update();
  }

private:
  QRectF colorArea() const
  {
    const qreal size = qMax(1, qMin(width(), height()) - 14);
    return QRectF((width() - size) * 0.5, (height() - size) * 0.5, size, size);
  }
  void updateFromPoint(const QPointF &point)
  {
    const QPointF sv = CBufferSVAtPoint(colorArea(), point);
    if(changed)
      changed(sv.x(), sv.y());
  }
  qreal m_Hue = 0.0, m_Saturation = 0.0, m_Value = 0.0;
};

class CBufferHueSlider : public QSlider
{
public:
  explicit CBufferHueSlider(QWidget *parent) : QSlider(Qt::Vertical, parent)
  {
    setObjectName(QStringLiteral("cbufferColorHue"));
    setRange(0, 3599);
    setSingleStep(10);
    setPageStep(100);
    setInvertedAppearance(true);
    setInvertedControls(true);
    setFixedWidth(36);
    setMinimumHeight(220);
    setFocusPolicy(Qt::StrongFocus);
    setAccessibleName(QCoreApplication::translate("BufferViewer", "Hue"));
  }

protected:
  void paintEvent(QPaintEvent *) override
  {
    QPainter painter(this);
    const QRectF area = colorArea();
    QLinearGradient hue(area.topLeft(), area.bottomLeft());
    for(int i = 0; i <= 6; i++)
      hue.setColorAt(i / 6.0, QColor::fromHsvF(i == 6 ? 0.0 : i / 6.0, 1.0, 1.0));
    painter.fillRect(area, hue);
    painter.setPen(QPen(palette().color(hasFocus() ? QPalette::Highlight : QPalette::Mid),
                        hasFocus() ? 2 : 1));
    painter.drawRect(area);
    const qreal y = area.top() + value() * area.height() / maximum();
    painter.setPen(QPen(Qt::black, 4));
    painter.drawLine(QPointF(area.left() - 3, y), QPointF(area.right() + 3, y));
    painter.setPen(QPen(Qt::white, 2));
    painter.drawLine(QPointF(area.left() - 3, y), QPointF(area.right() + 3, y));
  }
  void mousePressEvent(QMouseEvent *event) override
  {
    if(event->button() != Qt::LeftButton)
      return;
    setFocus(Qt::MouseFocusReason);
    setSliderDown(true);
    updateFromPoint(event->localPos());
  }
  void mouseMoveEvent(QMouseEvent *event) override
  {
    if(event->buttons() & Qt::LeftButton)
      updateFromPoint(event->localPos());
  }
  void mouseReleaseEvent(QMouseEvent *event) override
  {
    if(event->button() == Qt::LeftButton)
    {
      updateFromPoint(event->localPos());
      setSliderDown(false);
    }
  }

private:
  QRectF colorArea() const { return QRectF(rect()).adjusted(7, 7, -7, -7); }
  void updateFromPoint(const QPointF &point)
  {
    const QRectF area = colorArea();
    setValue(
        qRound(qBound(0.0, (point.y() - area.top()) / qMax(1.0, area.height()), 1.0) * maximum()));
  }
};

class CBufferColorDialog : public QDialog
{
public:
  CBufferColorDialog(const QColor &initial, QWidget *parent) : QDialog(parent)
  {
    setObjectName(QStringLiteral("cbufferColorDialog"));
    QVBoxLayout *layout = new QVBoxLayout(this);
    QHBoxLayout *colors = new QHBoxLayout;
    m_SV = new CBufferSaturationValue(this);
    m_Hue = new CBufferHueSlider(this);
    colors->addWidget(m_SV, 1);
    colors->addWidget(m_Hue);
    layout->addLayout(colors, 1);
    QHBoxLayout *numbers = new QHBoxLayout;
    const QString labels[] = {QCoreApplication::translate("BufferViewer", "Hue"),
                              QCoreApplication::translate("BufferViewer", "Saturation"),
                              QCoreApplication::translate("BufferViewer", "Brightness")};
    for(int i = 0; i < 3; i++)
    {
      m_Values[i] = new QDoubleSpinBox(this);
      m_Values[i]->setObjectName(QStringLiteral("cbufferColorHSV%1").arg(i));
      m_Values[i]->setRange(0.0, i == 0 ? 359.9 : 100.0);
      m_Values[i]->setDecimals(i == 0 ? 1 : 2);
      m_Values[i]->setSingleStep(1.0);
      m_Values[i]->setKeyboardTracking(false);
      m_Values[i]->setAccessibleName(labels[i]);
      QLabel *label = new QLabel(labels[i], this);
      label->setBuddy(m_Values[i]);
      QVBoxLayout *column = new QVBoxLayout;
      column->addWidget(label);
      column->addWidget(m_Values[i]);
      numbers->addLayout(column);
      QObject::connect(m_Values[i], QOverload<double>::of(&QDoubleSpinBox::valueChanged), this,
                       [this](double) {
                         updateColor(m_Values[0]->value() / 360.0, m_Values[1]->value() / 100.0,
                                     m_Values[2]->value() / 100.0);
                       });
    }
    layout->addLayout(numbers);
    m_Preview = new QWidget(this);
    m_Preview->setObjectName(QStringLiteral("cbufferColorPreview"));
    m_Preview->setMinimumHeight(24);
    m_Preview->setAutoFillBackground(true);
    layout->addWidget(m_Preview);
    QLabel *hint = new QLabel(
        QCoreApplication::translate(
            "BufferViewer",
            "HDR intensity and alpha stay unchanged. Cancel restores the starting color."),
        this);
    hint->setWordWrap(true);
    layout->addWidget(hint);
    QDialogButtonBox *buttons =
        new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, this);
    layout->addWidget(buttons);
    QObject::connect(buttons, &QDialogButtonBox::accepted, this, &QDialog::accept);
    QObject::connect(buttons, &QDialogButtonBox::rejected, this, &QDialog::reject);
    QObject::connect(m_Hue, &QSlider::valueChanged, this, [this](int hue) {
      updateColor(hue / 3600.0, m_Values[1]->value() / 100.0, m_Values[2]->value() / 100.0);
    });
    m_SV->changed = [this](qreal saturation, qreal value) {
      updateColor(m_Values[0]->value() / 360.0, saturation, value);
    };
    updateColor(qMax(0.0, initial.hsvHueF()), initial.hsvSaturationF(), initial.valueF());
    resize(360, 390);
    m_SV->setFocus();
  }

  std::function<void(const QColor &)> colorChanged;

private:
  void updateColor(qreal hue, qreal saturation, qreal value)
  {
    const QSignalBlocker hueBlocker(m_Hue), hBlocker(m_Values[0]), sBlocker(m_Values[1]),
        vBlocker(m_Values[2]);
    m_Hue->setValue(qRound(hue * 3600.0));
    m_Values[0]->setValue(hue * 360.0);
    m_Values[1]->setValue(saturation * 100.0);
    m_Values[2]->setValue(value * 100.0);
    m_SV->setHSV(hue, saturation, value);
    const QColor color = QColor::fromHsvF(hue, saturation, value);
    QPalette preview = m_Preview->palette();
    preview.setColor(QPalette::Window, color);
    m_Preview->setPalette(preview);
    if(colorChanged)
      colorChanged(color);
  }

  CBufferSaturationValue *m_SV = NULL;
  CBufferHueSlider *m_Hue = NULL;
  QDoubleSpinBox *m_Values[3] = {};
  QWidget *m_Preview = NULL;
};

struct CBufferLiveEdit
{
  QWidget *panel = NULL;
  QLabel *name = NULL;
  QLabel *status = NULL;
  QDoubleSpinBox *values[4] = {};
  QLabel *channelLabels[4] = {};
  QSlider *sliders[4] = {};
  QWidget *channels[4] = {};
  QPushButton *reset = NULL;
  QPushButton *color = NULL;
  QTimer *timer = NULL;
  QPointer<CBufferColorDialog> colorDialog;
  double dragBase[4] = {};
  QList<quint64> offsets;
  VarType type = VarType::Unknown;
  QString key;
  QString scope;
  QString shaderHash;
  ResourceId buffer;
  uint64_t bindingOffset = 0;
  uint64_t bindingSize = 0;
  uint32_t eventId = 0;
  uint32_t blockIndex = 0;
  uint32_t arrayIndex = 0;
  ShaderStage stage = ShaderStage::Count;
  QMap<quint64, uint32_t> currentBits;
  QMap<QString, QMap<quint64, uint32_t>> pending;
  QSet<QString> resets;
  std::shared_ptr<std::atomic<uint64_t>> epoch = std::make_shared<std::atomic<uint64_t>>(0);
  uint64_t generation = 0;
  bool generationKnown = false;
  bool inFlight = false;
  bool publishing = false;
  bool settingValues = false;
  QElapsedTimer oldestInput;
  QElapsedTimer newestInput;
  uint32_t queuedSamples = 0;
};

// Pinning never changes the application's selected event. A private read/edit may visit the
// pinned event on the replay thread, but must put the driver back before publishing anything.
template <typename Controller>
struct ScopedCBufferReplayEvent
{
  Controller *controller;
  uint32_t previous;
  bool changed;
  ScopedCBufferReplayEvent(Controller *r, uint32_t eventId)
      : controller(r), previous(r->GetCurrentEventID()), changed(eventId && previous != eventId)
  {
    if(changed)
      controller->SetFrameEvent(eventId, true);
  }
  ScopedCBufferReplayEvent(const ScopedCBufferReplayEvent &) = delete;
  ScopedCBufferReplayEvent &operator=(const ScopedCBufferReplayEvent &) = delete;
  void Restore()
  {
    if(changed)
    {
      changed = false;
      controller->SetFrameEvent(previous, true);
    }
  }
  ~ScopedCBufferReplayEvent() { Restore(); }
};

using CBufferReplayEventScope = ScopedCBufferReplayEvent<IReplayController>;

static bool CBufferLiveInputActive(const CBufferLiveEdit &live)
{
  for(int component = 0; component < live.offsets.size(); component++)
  {
    if(live.sliders[component] && live.sliders[component]->isSliderDown())
      return true;
    const QDoubleSpinBox *spin = live.values[component];
    const QLineEdit *text = spin ? spin->findChild<QLineEdit *>() : NULL;
    if(spin && spin->hasFocus() && text && text->isModified())
      return true;
  }
  return false;
}

static QString CBufferLiveErrorText(const QString &error)
{
  // Native diagnostics are stable protocol text. Keep the original in the tooltip.
  if(error == QLatin1String("Vulkan buffer patch binding overlaps an image binding or has incomplete image memory evidence"))
    return QCoreApplication::translate("BufferViewer", "The value was not applied: this buffer overlaps a texture, or its memory binding could not be verified.");
  if(error == QLatin1String("Vulkan buffer patch has no safe initial-state restore range inside its binding"))
    return QCoreApplication::translate("BufferViewer", "The value was not applied: the original bytes cannot be safely restored for this range.");
  if(error.startsWith(QLatin1String("Vulkan buffer patches do not support buffer ownership transfers, imported memory, device-group or disjoint image-plane bindings: ")))
    return QCoreApplication::translate("BufferViewer", "The value was not applied: this memory binding needs an unsupported replay path. See the tooltip for details.");
  if(error == QLatin1String("OpenGL persistent buffer patch replay failed"))
    return QCoreApplication::translate("BufferViewer", "The value was not applied: OpenGL could not replay the buffer update. See the tooltip for details.");
  return error;
}

static uint64_t CBufferReflectedReadSize(uint64_t bindingSize, uint64_t reflectedSize)
{
  // A D3D12 root CBV reports the rest of its upload-ring allocation, not the size of this
  // declaration. Reading megabytes past the block is unnecessary for the reflected view.
  // Unknown reflection retains the original range; custom formats do not use this helper.
  return reflectedSize > 0 ? qMin(bindingSize, reflectedSize) : bindingSize;
}

static bool CBufferColorField(const QString &name, VarType type, int components)
{
  if(type != VarType::Float || components < 3 || components > 4)
    return false;
  const QString lower = name.toLower();
  return lower.contains(QLatin1String("color")) || lower.contains(QLatin1String("colour")) ||
         lower.contains(QLatin1String("tint")) || lower.contains(QLatin1String("albedo"));
}

static bool CBufferLiveLaneBits(VarType type, double value, uint32_t &bits)
{
  if(!std::isfinite(value))
    return false;
  if(type == VarType::Float && value >= -FLT_MAX && value <= FLT_MAX)
  {
    const float converted = float(value);
    memcpy(&bits, &converted, sizeof(bits));
    return true;
  }
  if(type == VarType::UInt && value >= 0.0 && value <= double(UINT32_MAX) && floor(value) == value)
  {
    bits = uint32_t(value);
    return true;
  }
  if(type == VarType::SInt && value >= double(INT32_MIN) && value <= double(INT32_MAX) &&
     floor(value) == value)
  {
    const int32_t converted = int32_t(value);
    memcpy(&bits, &converted, sizeof(bits));
    return true;
  }
  return false;
}

struct CBufferLiveRun
{
  quint64 offset = 0;
  bytebuf bytes;
};

static QList<CBufferLiveRun> CBufferLiveRuns(const QMap<quint64, uint32_t> &lanes)
{
  QList<CBufferLiveRun> result;
  auto lane = lanes.begin();
  while(lane != lanes.end())
  {
    CBufferLiveRun run;
    run.offset = lane.key();
    while(lane != lanes.end() && lane.key() == run.offset + run.bytes.size())
    {
      const uint32_t bits = lane.value();
      const size_t previous = run.bytes.size();
      run.bytes.resize(previous + 4);
      memcpy(run.bytes.data() + previous, &bits, 4);
      ++lane;
    }
    result.push_back(run);
  }
  return result;
}

enum class ReplayLabMaterialGate
{
  Available,
  UnsupportedAPI,
  NotReplayableAction,
  ViewNotReady,
  MissingReflection,
  NoRuntimeBacking,
  RootConstants,
  CompileConstants,
  CustomFormat,
  UnsupportedLeaf,
  ShaderUnbound,
  BufferUnbound,
  InvalidBindingWindow,
  UnsupportedBinding,
};

struct ReplayLabMaterialGateState
{
  GraphicsAPI api = GraphicsAPI::D3D11;
  bool remoteReplay = false;
  bool replayableAction = true;
  bool viewReady = true;
  bool reflectionAvailable = true;
  bool bytesBacked = true;
  bool inlineData = false;
  bool rootCBV = false;
  bool indirectAction = false;
  bool compileConstants = false;
  bool customFormat = false;
  bool materialLeaf = true;
  bool shaderBound = true;
  bool bufferBound = true;
  uint64_t byteOffset = 0;
  uint64_t byteSize = 16;
  uint32_t physicalSlot = 0;
  uint32_t registerSpace = 0;
  uint32_t bindArraySize = 1;
  uint32_t arrayIndex = 0;
};

static uint64_t ReplayLabMaterialBindingByteSize(GraphicsAPI api, uint64_t reportedByteSize,
                                                 uint32_t reflectedByteSize, bool rootCBV)
{
  // A D3D12 root CBV has no SizeInBytes. The generic pipeline state therefore exposes the
  // resource remainder, which is often a large upload ring. Only reflection defines the safe,
  // shader-visible editing window. Descriptor-table CBVs retain their explicit SizeInBytes.
  if(api == GraphicsAPI::D3D12 && rootCBV)
    return AlignUp(uint64_t(reflectedByteSize), uint64_t(16));
  return reportedByteSize;
}

static ReplayLabMaterialGate GetReplayLabMaterialGate(const ReplayLabMaterialGateState &state)
{
  if(state.api != GraphicsAPI::D3D11 && state.api != GraphicsAPI::D3D12 &&
     state.api != GraphicsAPI::OpenGL && state.api != GraphicsAPI::Vulkan)
    return ReplayLabMaterialGate::UnsupportedAPI;
  if(!state.replayableAction)
    return ReplayLabMaterialGate::NotReplayableAction;
  if(!state.viewReady)
    return ReplayLabMaterialGate::ViewNotReady;
  if(!state.reflectionAvailable)
    return ReplayLabMaterialGate::MissingReflection;
  if(!state.bytesBacked)
    return ReplayLabMaterialGate::NoRuntimeBacking;
  if(state.inlineData)
    return ReplayLabMaterialGate::RootConstants;
  if(state.compileConstants)
    return ReplayLabMaterialGate::CompileConstants;
  if(state.customFormat)
    return ReplayLabMaterialGate::CustomFormat;
  if(!state.materialLeaf)
    return ReplayLabMaterialGate::UnsupportedLeaf;
  if(!state.shaderBound)
    return ReplayLabMaterialGate::ShaderUnbound;
  if(!state.bufferBound)
    return ReplayLabMaterialGate::BufferUnbound;
  if(state.byteSize == 0 || state.byteSize == ~0ULL || state.byteOffset > ~0ULL - state.byteSize)
    return ReplayLabMaterialGate::InvalidBindingWindow;
  // The shared edit addresses the selected storage range, not a stage/slot override. Descriptor
  // arrays and remote replay use the same identity; the native backend validates patch support.
  if(state.bindArraySize != 0 && state.arrayIndex >= state.bindArraySize)
    return ReplayLabMaterialGate::UnsupportedBinding;
  if((state.api == GraphicsAPI::D3D11 || state.api == GraphicsAPI::D3D12) &&
     (state.byteSize > 64 * 1024 || state.byteOffset % 16 != 0 || state.byteSize % 16 != 0))
    return ReplayLabMaterialGate::InvalidBindingWindow;
  if(state.api == GraphicsAPI::D3D11 &&
     (state.physicalSlot >= 14 || state.registerSpace != 0))
    return ReplayLabMaterialGate::UnsupportedBinding;
  if(state.api == GraphicsAPI::D3D12 && state.byteOffset % 256 != 0)
    return ReplayLabMaterialGate::InvalidBindingWindow;
  return ReplayLabMaterialGate::Available;
}

static const uint32_t MaxVisibleRows = 10000;

class CameraWrapper
{
public:
  CameraWrapper(ICaptureContext &ctx) : m_Ctx(ctx) {}
  virtual ~CameraWrapper() {}
  virtual bool Update(QRect winSize) = 0;
  virtual ICamera *camera() = 0;
  virtual void Frame(const QVector3D &center, float distance) = 0;
  virtual void DollyZoom(float ratio) = 0;
  virtual float ViewDepth(const QVector3D &point) const = 0;
  virtual void RemapPivot(const QMatrix4x4 &mapping) = 0;
  virtual void Orient(float yaw, float pitch, float roll = 0) = 0;

  virtual void MouseMove(QMouseEvent *e)
  {
    if(e->buttons() & Qt::LeftButton)
    {
      m_DragStartPos = e->pos();
    }
    else
    {
      m_DragStartPos = QPoint(-1, -1);
    }
  }

  KeyPressDirection GetDirection(QKeyEvent *e)
  {
    const rdcarray<uint32_t> &keys = m_Ctx.Config().MeshViewer_KeySettings;
    for(int i = 0; i < (int)KeyPressDirection::Count; i++)
    {
      KeyPressDirection dir = KeyPressDirection(i);
      Qt::Key primary, secondary;

      int p = keySettingIdx(dir, true);
      int s = keySettingIdx(dir, false);

      if(p < keys.count() && keys[p] != 0)
        primary = getKeySetting(keys[p]);
      else
        primary = getDefaultKey(dir, true);

      if(s < keys.count() && keys[s] != 0)
        secondary = getKeySetting(keys[s]);
      else
        secondary = getDefaultKey(dir, false);

      if(e->key() == primary || e->key() == secondary)
        return dir;
    }

    return KeyPressDirection::None;
  }

  KeyPressDirection GetDirection(QMouseEvent *e)
  {
    if(m_Ctx.Config().MeshViewer_KeySettings.size() >= (size_t)KeyPressDirection::NumSettings)
    {
      for(int i = 0; i < (int)KeyPressDirection::Count; i++)
      {
        KeyPressDirection dir = KeyPressDirection(i);
        Qt::MouseButton primary =
            getMouseButtonSetting(m_Ctx.Config().MeshViewer_KeySettings[keySettingIdx(dir, true)]);
        Qt::MouseButton secondary =
            getMouseButtonSetting(m_Ctx.Config().MeshViewer_KeySettings[keySettingIdx(dir, false)]);

        if(e->button() == primary || e->button() == secondary)
          return dir;
      }
    }

    return KeyPressDirection::None;
  }

  KeyPressDirection GetDirection(QWheelEvent *e)
  {
    if(m_Ctx.Config().MeshViewer_KeySettings.size() >= (size_t)KeyPressDirection::NumSettings)
    {
      QPoint angleDelta = e->angleDelta();
      angleDelta.setX(qMin(1, qMax(-1, angleDelta.x())));
      angleDelta.setY(qMin(1, qMax(-1, angleDelta.y())));

      for(int i = 0; i < (int)KeyPressDirection::Count; i++)
      {
        KeyPressDirection dir = KeyPressDirection(i);
        QPoint primary =
            getMouseWheelSetting(m_Ctx.Config().MeshViewer_KeySettings[keySettingIdx(dir, true)]);
        QPoint secondary =
            getMouseWheelSetting(m_Ctx.Config().MeshViewer_KeySettings[keySettingIdx(dir, false)]);

        if(angleDelta == primary || angleDelta == secondary)
          return dir;
      }
    }

    return KeyPressDirection::None;
  }

  virtual void KeyUp(QKeyEvent *e)
  {
    KeyPressDirection dir = GetDirection(e);

    if(dir == KeyPressDirection::Left || dir == KeyPressDirection::Right)
      setMove(Direction::Horiz, 0);
    else if(dir == KeyPressDirection::Forward || dir == KeyPressDirection::Back)
      setMove(Direction::Fwd, 0);
    else if(dir == KeyPressDirection::Up || dir == KeyPressDirection::Down)
      setMove(Direction::Vert, 0);

    Qt::KeyboardModifier speedMod = Qt::ShiftModifier;
    if(m_Ctx.Config().MeshViewer_SpeedModifier > 0)
      speedMod = Qt::KeyboardModifier(m_Ctx.Config().MeshViewer_SpeedModifier);

    if(speedMod != Qt::NoModifier && (e->modifiers() & speedMod))
      m_CurrentSpeed = 3.0f;
    else
      m_CurrentSpeed = 1.0f;
  }

  virtual void KeyDown(QKeyEvent *e)
  {
    KeyPressDirection dir = GetDirection(e);

    switch(dir)
    {
      default: break;
      case KeyPressDirection::Left: setMove(Direction::Horiz, -1); break;
      case KeyPressDirection::Right: setMove(Direction::Horiz, 1); break;
      case KeyPressDirection::Forward: setMove(Direction::Fwd, 1); break;
      case KeyPressDirection::Back: setMove(Direction::Fwd, -1); break;
      case KeyPressDirection::Up: setMove(Direction::Vert, 1); break;
      case KeyPressDirection::Down: setMove(Direction::Vert, -1); break;
    }

    Qt::KeyboardModifier speedMod = Qt::ShiftModifier;
    if(m_Ctx.Config().MeshViewer_SpeedModifier > 0)
      speedMod = Qt::KeyboardModifier(m_Ctx.Config().MeshViewer_SpeedModifier);

    if(speedMod != Qt::NoModifier && (e->modifiers() & speedMod))
      m_CurrentSpeed = 3.0f;
    else
      m_CurrentSpeed = 1.0f;
  }

  virtual void MouseClick(QMouseEvent *e)
  {
    m_DragStartPos = e->pos();
    m_Orbiting = false;
    KeyPressDirection dir = GetDirection(e);

    switch(dir)
    {
      default: break;
      case KeyPressDirection::Left: setMove(Direction::Horiz, -1); break;
      case KeyPressDirection::Right: setMove(Direction::Horiz, 1); break;
      case KeyPressDirection::Forward: setMove(Direction::Fwd, 1); break;
      case KeyPressDirection::Back: setMove(Direction::Fwd, -1); break;
      case KeyPressDirection::Up: setMove(Direction::Vert, 1); break;
      case KeyPressDirection::Down: setMove(Direction::Vert, -1); break;
    }
  }

  virtual void MouseUnclick(QMouseEvent *e)
  {
    m_Orbiting = false;
    KeyPressDirection dir = GetDirection(e);

    if(dir == KeyPressDirection::Left || dir == KeyPressDirection::Right)
      setMove(Direction::Horiz, 0);
    else if(dir == KeyPressDirection::Forward || dir == KeyPressDirection::Back)
      setMove(Direction::Fwd, 0);
    else if(dir == KeyPressDirection::Up || dir == KeyPressDirection::Down)
      setMove(Direction::Vert, 0);
  }

  virtual void MouseWheel(QWheelEvent *e) {}

  float SpeedMultiplier = 0.05f;

protected:
  enum class Direction
  {
    Fwd,
    Horiz,
    Vert,
    Num
  };

  int move(Direction dir) { return m_CurrentMove[(int)dir]; }
  float currentSpeed() { return m_CurrentSpeed * SpeedMultiplier; }
  QPoint dragStartPos() { return m_DragStartPos; }

  MeshCameraGesture cameraGesture(QMouseEvent *e)
  {
    const MeshCameraGesture gesture =
        meshCameraGesture(m_Ctx.Config().MeshViewer_CameraNavigation, e->buttons(), e->modifiers());
    if(gesture == MeshCameraGesture::Orbit && !m_Orbiting)
      m_InvertedOrbit = camera()->GetUp().y < -1e-6f;
    m_Orbiting = gesture == MeshCameraGesture::Orbit;
    return gesture;
  }

  void orbit(const QPoint &delta)
  {
    const float radiansPerPixel = .006f;
    RENDERDOC_RotateMeshPreviewOrbit(camera(), delta.x() * radiansPerPixel,
                                     delta.y() * radiansPerPixel, m_InvertedOrbit);
  }

  ICaptureContext &m_Ctx;
private:
  float m_CurrentSpeed = 1.0f;
  int m_CurrentMove[(int)Direction::Num] = {0, 0, 0};

  void setMove(Direction dir, int val) { m_CurrentMove[(int)dir] = val; }
  QPoint m_DragStartPos = QPoint(-1, -1);
  bool m_Orbiting = false, m_InvertedOrbit = false;
};

class ArcballWrapper : public CameraWrapper
{
public:
  ArcballWrapper(ICaptureContext &ctx) : CameraWrapper(ctx)
  {
    m_Cam = RENDERDOC_InitCamera(CameraType::Arcball);
  }
  virtual ~ArcballWrapper() { m_Cam->Shutdown(); }
  ICamera *camera() override { return m_Cam; }
  void Reset(FloatVector pos, float dist)
  {
    m_Cam->ResetArcball();

    setLookAtPos(pos);
    SetDistance(dist);
  }

  void Frame(const QVector3D &center, float distance) override
  {
    setLookAtPos(FloatVector(center.x(), center.y(), center.z(), 1.0f));
    if(distance > 0.0f)
      SetDistance(distance);
  }

  void DollyZoom(float ratio) override { SetDistance(m_Distance * ratio); }
  void Orient(float yaw, float pitch, float roll) override
  {
    RENDERDOC_SetMeshPreviewOrbit(m_Cam, yaw, pitch);
    if(roll != 0)
      m_Cam->RotateArcball(1, 0, 2 * std::cos(roll), 2 * std::sin(roll));
  }
  void RemapPivot(const QMatrix4x4 &mapping) override
  {
    const QVector3D pivot = mapping.map(QVector3D(m_LookAt.x, m_LookAt.y, m_LookAt.z));
    setLookAtPos(FloatVector(pivot.x(), pivot.y(), pivot.z(), 1.0f));
  }
  float ViewDepth(const QVector3D &point) const override
  {
    const FloatVector fwd = m_Cam->GetForward();
    return m_Distance + QVector3D::dotProduct(point - QVector3D(m_LookAt.x, m_LookAt.y, m_LookAt.z),
                                              QVector3D(fwd.x, fwd.y, fwd.z));
  }

  void SetDistance(float dist)
  {
    m_Distance = qAbs(dist);
    m_Cam->SetArcballDistance(m_Distance);
  }

  bool Update(QRect size) override
  {
    m_WinSize = size;
    return false;
  }

  void MouseWheel(QWheelEvent *e) override
  {
    CameraWrapper::MouseWheel(e);

    float mod = (1.0f - e->delta() / 2500.0f);

    SetDistance(qMax(1e-6f, m_Distance * mod));
  }

  void MouseMove(QMouseEvent *e) override
  {
    const MeshCameraGesture gesture = cameraGesture(e);
    if(m_LastPointer != QPoint(-1, -1))
    {
      if(gesture == MeshCameraGesture::Pan)
      {
        const float scale = 2.0f * m_Distance *
                            std::tan(qDegreesToRadians(m_Ctx.Config().MeshViewer_CameraFov * .5f)) /
                            qMax(1, m_WinSize.height());
        const float xdelta = (e->pos().x() - m_LastPointer.x()) * scale;
        const float ydelta = (e->pos().y() - m_LastPointer.y()) * scale;

        FloatVector right = m_Cam->GetRight();
        FloatVector up = m_Cam->GetUp();

        m_LookAt.x -= right.x * xdelta;
        m_LookAt.y -= right.y * xdelta;
        m_LookAt.z -= right.z * xdelta;

        m_LookAt.x += up.x * ydelta;
        m_LookAt.y += up.y * ydelta;
        m_LookAt.z += up.z * ydelta;

        m_Cam->SetPosition(m_LookAt.x, m_LookAt.y, m_LookAt.z);
      }
      else if(gesture == MeshCameraGesture::Orbit)
        orbit(e->pos() - m_LastPointer);
      else if(gesture == MeshCameraGesture::Dolly)
      {
        const QPoint delta = e->pos() - m_LastPointer;
        const int movement =
            m_Ctx.Config().MeshViewer_CameraNavigation == 1 ? -delta.y() : delta.x() + delta.y();
        SetDistance(qMax(1e-6f, m_Distance * std::exp(-movement * .01f)));
      }
    }
    m_LastPointer = e->buttons() == Qt::NoButton ? QPoint(-1, -1) : e->pos();
    CameraWrapper::MouseMove(e);
  }

  void MouseClick(QMouseEvent *e) override
  {
    m_LastPointer = e->pos();
    CameraWrapper::MouseClick(e);
  }

  FloatVector lookAtPos() { return m_LookAt; }
  void setLookAtPos(const FloatVector &v)
  {
    m_LookAt = v;
    m_Cam->SetPosition(v.x, v.y, v.z);
  }

private:
  ICamera *m_Cam;

  QRect m_WinSize;

  float m_Distance = 10.0f;
  FloatVector m_LookAt;
  QPoint m_LastPointer = QPoint(-1, -1);
};

class FlycamWrapper : public CameraWrapper
{
public:
  FlycamWrapper(ICaptureContext &ctx) : CameraWrapper(ctx)
  {
    m_Cam = RENDERDOC_InitCamera(CameraType::FPSLook);
  }
  virtual ~FlycamWrapper() { m_Cam->Shutdown(); }
  ICamera *camera() override { return m_Cam; }
  void Orient(float yaw, float pitch, float roll) override
  {
    m_Cam->SetFPSRotation(pitch, yaw, roll);
  }
  void Frame(const QVector3D &center, float distance) override
  {
    const FloatVector basis = m_Cam->GetForward();
    const QVector3D forward(basis.x, basis.y, basis.z);
    if(distance <= 0.0f)
    {
      const QVector3D position(m_Position.x, m_Position.y, m_Position.z);
      distance = qMax(8.0f * m_Ctx.Config().MeshViewer_CameraNear,
                      QVector3D::dotProduct(center - position, forward));
    }
    m_FocusDistance = distance;
    const QVector3D position = center - distance * forward;
    m_Position = FloatVector(position.x(), position.y(), position.z(), 1.0f);
    m_Cam->SetPosition(position.x(), position.y(), position.z());
  }

  float ViewDepth(const QVector3D &point) const override
  {
    const FloatVector fwd = m_Cam->GetForward();
    return QVector3D::dotProduct(point - QVector3D(m_Position.x, m_Position.y, m_Position.z),
                                 QVector3D(fwd.x, fwd.y, fwd.z));
  }

  void RemapPivot(const QMatrix4x4 &mapping) override
  {
    const FloatVector fwd = m_Cam->GetForward();
    const QVector3D position(m_Position.x, m_Position.y, m_Position.z);
    const QVector3D pivot = position + m_FocusDistance * QVector3D(fwd.x, fwd.y, fwd.z);
    const QVector3D shifted = position + mapping.map(pivot) - pivot;
    m_Position = FloatVector(shifted.x(), shifted.y(), shifted.z(), 1.0f);
    m_Cam->SetPosition(m_Position.x, m_Position.y, m_Position.z);
  }

  void DollyZoom(float ratio) override
  {
    const FloatVector forward = m_Cam->GetForward();
    const float movement = m_FocusDistance * (1.0f - ratio);
    m_Position.x += forward.x * movement;
    m_Position.y += forward.y * movement;
    m_Position.z += forward.z * movement;
    m_FocusDistance *= ratio;
    m_Cam->SetPosition(m_Position.x, m_Position.y, m_Position.z);
  }

  void Reset(FloatVector pos)
  {
    m_Position = pos;

    m_Cam->SetPosition(m_Position.x, m_Position.y, m_Position.z);
    m_Cam->SetFPSRotation(0.0f, 0.0f, 0.0f);
  }

  bool Update(QRect size) override
  {
    m_WinSize = size;
    FloatVector fwd = m_Cam->GetForward();
    FloatVector right = m_Cam->GetRight();

    float speed = currentSpeed();

    int horizMove = move(CameraWrapper::Direction::Horiz);
    if(horizMove)
    {
      m_Position.x += right.x * speed * (float)horizMove;
      m_Position.y += right.y * speed * (float)horizMove;
      m_Position.z += right.z * speed * (float)horizMove;
    }

    int vertMove = move(CameraWrapper::Direction::Vert);
    if(vertMove)
    {
      // this makes less intuitive sense, instead go 'absolute' up
      // m_Position.x += up.x * speed * (float)vertMove;
      // m_Position.y += up.y * speed * (float)vertMove;
      // m_Position.z += up.z * speed * (float)vertMove;

      m_Position.y += speed * (float)vertMove;
    }

    int fwdMove = move(CameraWrapper::Direction::Fwd);
    if(fwdMove)
    {
      m_FocusDistance = qMax(8.0f * m_Ctx.Config().MeshViewer_CameraNear,
                             m_FocusDistance - speed * float(fwdMove));
      m_Position.x += fwd.x * speed * (float)fwdMove;
      m_Position.y += fwd.y * speed * (float)fwdMove;
      m_Position.z += fwd.z * speed * (float)fwdMove;
    }

    if(horizMove || vertMove || fwdMove)
    {
      m_Cam->SetPosition(m_Position.x, m_Position.y, m_Position.z);
      return true;
    }

    return false;
  }

  virtual void MouseWheel(QWheelEvent *e) override
  {
    CameraWrapper::MouseWheel(e);

    KeyPressDirection dir = GetDirection(e);

    FloatVector fwd = m_Cam->GetForward();
    FloatVector right = m_Cam->GetRight();

    float speed = currentSpeed();

    if(dir == KeyPressDirection::Left || dir == KeyPressDirection::Right)
    {
      int horizMove = dir == KeyPressDirection::Left ? -1 : 1;
      m_Position.x += right.x * speed * (float)horizMove;
      m_Position.y += right.y * speed * (float)horizMove;
      m_Position.z += right.z * speed * (float)horizMove;
    }
    else if(dir == KeyPressDirection::Up || dir == KeyPressDirection::Down)
    {
      // this makes less intuitive sense, instead go 'absolute' up
      // m_Position.x += up.x * speed * (float)vertMove;
      // m_Position.y += up.y * speed * (float)vertMove;
      // m_Position.z += up.z * speed * (float)vertMove;

      int vertMove = dir == KeyPressDirection::Up ? -1 : 1;
      m_Position.y += speed * (float)vertMove;
    }
    else if(dir == KeyPressDirection::Forward || dir == KeyPressDirection::Back)
    {
      int fwdMove = dir == KeyPressDirection::Back ? -1 : 1;
      m_Position.x += fwd.x * speed * (float)fwdMove;
      m_Position.y += fwd.y * speed * (float)fwdMove;
      m_Position.z += fwd.z * speed * (float)fwdMove;
    }
    else
    {
      return;
    }

    m_Cam->SetPosition(m_Position.x, m_Position.y, m_Position.z);
  }

  void MouseClick(QMouseEvent *e) override
  {
    m_LastPointer = e->pos();
    CameraWrapper::MouseClick(e);
  }

  void MouseMove(QMouseEvent *e) override
  {
    const MeshCameraGesture gesture = cameraGesture(e);
    if(m_LastPointer != QPoint(-1, -1))
    {
      const QPoint delta = e->pos() - m_LastPointer;
      if(gesture == MeshCameraGesture::Orbit)
        orbit(delta);
      else if(gesture == MeshCameraGesture::Pan)
      {
        const float scale = 2.0f * m_FocusDistance *
                            std::tan(qDegreesToRadians(m_Ctx.Config().MeshViewer_CameraFov * .5f)) /
                            qMax(1, m_WinSize.height());
        const FloatVector right = m_Cam->GetRight(), up = m_Cam->GetUp();
        m_Position.x += scale * (up.x * delta.y() - right.x * delta.x());
        m_Position.y += scale * (up.y * delta.y() - right.y * delta.x());
        m_Position.z += scale * (up.z * delta.y() - right.z * delta.x());
        m_Cam->SetPosition(m_Position.x, m_Position.y, m_Position.z);
      }
      else if(gesture == MeshCameraGesture::Dolly)
      {
        const int movement =
            m_Ctx.Config().MeshViewer_CameraNavigation == 1 ? -delta.y() : delta.x() + delta.y();
        DollyZoom(std::exp(-movement * .01f));
      }
    }
    m_LastPointer = e->buttons() == Qt::NoButton ? QPoint(-1, -1) : e->pos();
    CameraWrapper::MouseMove(e);
  }

private:
  ICamera *m_Cam;

  FloatVector m_Position;
  float m_FocusDistance = 10.0f;
  QRect m_WinSize;
  QPoint m_LastPointer = QPoint(-1, -1);
};

struct BufferData
{
  BufferData()
  {
    refcount.store(1);
    stride = 0;
  }

  void ref() { refcount.ref(); }
  void deref()
  {
    bool alive = refcount.deref();

    if(!alive)
      delete this;
  }

  size_t stride;
  bytebuf storage;
  QAtomicInteger<uint32_t> refcount;

  const byte *data() const { return storage.begin(); };
  const byte *end() const { return storage.end(); }
  bool hasData() const { return !storage.empty(); }
  size_t size() const { return storage.size(); }
};

struct BufferElementProperties
{
  ResourceFormat format;
  int buffer = 0;
  ShaderBuiltin systemValue = ShaderBuiltin::Undefined;
  bool perinstance = false;
  bool perprimitive = false;
  bool floatCastWrong = false;
  int instancerate = 1;
};

struct BufferConfiguration
{
  uint32_t curInstance = 0, curView = 0;
  uint32_t numRows = 0, unclampedNumRows = 0;
  uint32_t pagingOffset = 0;

  PackingRules packing;
  ShaderConstant fixedVars;
  rdcarray<ShaderVariable> evalVars;
  // Display identifiers from the current replacement, never decoding/editing declarations.
  rdcarray<ShaderConstant> cbufferDisplayVars;
  uint32_t repeatStride = 1;
  uint32_t repeatOffset = 0;

  QString statusString;

  bool noVertices = false;
  bool noInstances = false;

  // we can have two index buffers for VSOut data:
  // the original index buffer is used for the displayed value (in displayIndices), and the actual
  // potentially remapped or permuated index buffer used for fetching data (in indices).
  BufferData *displayIndices = NULL;
  int32_t displayBaseVertex = 0;
  BufferData *indices = NULL;
  int32_t baseVertex = 0;

  rdcfixedarray<uint32_t, 3> dispatchSize;
  rdcarray<TaskGroupSize> taskSizes;
  rdcarray<uint32_t> meshletVertexPrefixCounts;
  uint32_t taskOrMeshletOffset = 0;
  uint64_t perPrimitiveOffset = 0;
  uint32_t perPrimitiveStride = 0;
  Topology topology = Topology::TriangleList;

  rdcarray<ShaderConstant> columns;
  QMap<int, QString> channelAliases;
  rdcarray<BufferElementProperties> props;

  QVector<PixelValue> generics;
  QVector<bool> genericsEnabled;
  QList<BufferData *> buffers;
  uint32_t primRestart = 0;

  BufferConfiguration() = default;
  BufferConfiguration(const BufferConfiguration &o) = delete;
  ~BufferConfiguration() { reset(); }
  BufferConfiguration &operator=(const BufferConfiguration &o)
  {
    reset();

    curInstance = o.curInstance;
    numRows = o.numRows;
    unclampedNumRows = o.unclampedNumRows;
    pagingOffset = o.pagingOffset;

    packing = o.packing;
    fixedVars = o.fixedVars;
    evalVars = o.evalVars;
    cbufferDisplayVars = o.cbufferDisplayVars;
    repeatStride = o.repeatStride;
    repeatOffset = o.repeatOffset;

    statusString = o.statusString;
    channelAliases = o.channelAliases;

    noVertices = o.noVertices;
    noInstances = o.noInstances;

    displayIndices = o.displayIndices;
    if(displayIndices)
      displayIndices->ref();
    displayBaseVertex = o.displayBaseVertex;

    indices = o.indices;
    if(indices)
      indices->ref();

    baseVertex = o.baseVertex;
    meshletVertexPrefixCounts = o.meshletVertexPrefixCounts;
    dispatchSize = o.dispatchSize;
    taskSizes = o.taskSizes;
    taskOrMeshletOffset = o.taskOrMeshletOffset;
    perPrimitiveOffset = o.perPrimitiveOffset;
    perPrimitiveStride = o.perPrimitiveStride;
    topology = o.topology;

    columns = o.columns;
    props = o.props;
    generics = o.generics;
    genericsEnabled = o.genericsEnabled;
    primRestart = o.primRestart;

    buffers = o.buffers;
    for(BufferData *b : buffers)
      b->ref();

    return *this;
  }

  void reset()
  {
    if(indices)
      indices->deref();
    indices = NULL;

    if(displayIndices)
      displayIndices->deref();
    displayIndices = NULL;

    for(BufferData *b : buffers)
      b->deref();

    meshletVertexPrefixCounts.clear();
    dispatchSize = {};
    taskSizes.clear();

    buffers.clear();
    columns.clear();
    channelAliases.clear();
    props.clear();
    generics.clear();
    genericsEnabled.clear();
    cbufferDisplayVars.clear();
    numRows = 0;
    unclampedNumRows = 0;

    statusString.clear();

    noVertices = false;
    noInstances = false;
  }

  QString columnName(int col) const
  {
    if(col >= 0 && col < columns.count())
      return columns[col].name;

    return QString();
  }

  int guessPositionColumn() const
  {
    int posEl = -1;

    if(!columns.empty())
    {
      // prioritise system value over general "POSITION" string matching
      for(int i = 0; i < columns.count(); i++)
      {
        const BufferElementProperties &prop = props[i];

        if(prop.systemValue == ShaderBuiltin::Position)
        {
          posEl = i;
          break;
        }
      }

      // look for an exact match
      for(int i = 0; posEl == -1 && i < columns.count(); i++)
      {
        const ShaderConstant &el = columns[i];

        if(QString(el.name).compare(lit("POSITION"), Qt::CaseInsensitive) == 0 ||
           QString(el.name).compare(lit("POSITION0"), Qt::CaseInsensitive) == 0 ||
           QString(el.name).compare(lit("POS"), Qt::CaseInsensitive) == 0 ||
           QString(el.name).compare(lit("POS0"), Qt::CaseInsensitive) == 0)
        {
          posEl = i;
          break;
        }
      }

      // try anything containing position
      for(int i = 0; posEl == -1 && i < columns.count(); i++)
      {
        const ShaderConstant &el = columns[i];

        if(QString(el.name).contains(lit("POSITION"), Qt::CaseInsensitive))
        {
          posEl = i;
          break;
        }
      }

      // OK last resort, just look for 'pos'
      for(int i = 0; posEl == -1 && i < columns.count(); i++)
      {
        const ShaderConstant &el = columns[i];

        if(QString(el.name).contains(lit("POS"), Qt::CaseInsensitive))
        {
          posEl = i;
          break;
        }
      }

      // if we still have absolutely nothing, just use the first available element
      if(posEl == -1)
      {
        posEl = 0;
      }
    }

    return posEl;
  }

  int guessSecondaryColumn() const
  {
    int secondEl = -1;

    if(!columns.empty())
    {
      // prioritise TEXCOORD over general COLOR
      for(int i = 0; i < columns.count(); i++)
      {
        const ShaderConstant &el = columns[i];

        if(QString(el.name).compare(lit("TEXCOORD"), Qt::CaseInsensitive) == 0 ||
           QString(el.name).compare(lit("TEXCOORD0"), Qt::CaseInsensitive) == 0 ||
           QString(el.name).compare(lit("TEX"), Qt::CaseInsensitive) == 0 ||
           QString(el.name).compare(lit("TEX0"), Qt::CaseInsensitive) == 0 ||
           QString(el.name).compare(lit("UV"), Qt::CaseInsensitive) == 0 ||
           QString(el.name).compare(lit("UV0"), Qt::CaseInsensitive) == 0)
        {
          secondEl = i;
          break;
        }
      }

      for(int i = 0; secondEl == -1 && i < columns.count(); i++)
      {
        const ShaderConstant &el = columns[i];

        if(QString(el.name).compare(lit("COLOR"), Qt::CaseInsensitive) == 0 ||
           QString(el.name).compare(lit("COLOR0"), Qt::CaseInsensitive) == 0 ||
           QString(el.name).compare(lit("COL"), Qt::CaseInsensitive) == 0 ||
           QString(el.name).compare(lit("COL0"), Qt::CaseInsensitive) == 0)
        {
          secondEl = i;
          break;
        }
      }
    }

    return secondEl;
  }
};

uint32_t CalcIndex(BufferData *data, uint32_t vertID, int32_t baseVertex, uint32_t primRestart)
{
  const byte *idxData = data->data() + vertID * sizeof(uint32_t);
  if(idxData + sizeof(uint32_t) > data->end())
    return ~0U;

  uint32_t idx = *(const uint32_t *)idxData;

  // check for primitive restart *before* adding base vertex
  if(primRestart && idx == primRestart)
    return idx;

  // apply base vertex but clamp to 0 if subtracting
  if(baseVertex < 0)
  {
    uint32_t subtract = (uint32_t)(-baseVertex);

    if(idx < subtract)
      idx = 0;
    else
      idx -= subtract;
  }
  else if(baseVertex > 0)
  {
    idx += (uint32_t)baseVertex;
  }

  return idx;
}

static int columnGroupRole = Qt::UserRole + 10000;

static QString interpretVariant(const QVariant &v, const ShaderConstant &el,
                                const BufferElementProperties &prop)
{
  QString ret;

  QMetaType::Type vt = GetVariantMetatype(v);

  if(vt == QMetaType::Double)
  {
    double d = v.toDouble();
    // pad with space on left if sign is missing, to better align
    if(d < 0.0)
      ret = Formatter::Format(d);
    else if(d > 0.0)
      ret = lit(" ") + Formatter::Format(d);
    else if(qIsNaN(d))
      ret = lit(" NaN");
    else
      // force negative and positive 0 together
      ret = lit(" ") + Formatter::Format(0.0);
  }
  else if(vt == QMetaType::Float)
  {
    float f = v.toFloat();
    // pad with space on left if sign is missing, to better align
    if(f < 0.0)
      ret = Formatter::Format(f);
    else if(f > 0.0)
      ret = lit(" ") + Formatter::Format(f);
    else if(qIsNaN(f))
      ret = lit(" NaN");
    else
      // force negative and positive 0 together
      ret = lit(" ") + Formatter::Format(0.0);
  }
  else if(vt == QMetaType::UInt || vt == QMetaType::UShort || vt == QMetaType::UChar)
  {
    uint32_t u = v.toUInt();

    if(prop.floatCastWrong)
    {
      float f = (float)u;
      memcpy(&u, &f, sizeof(f));
    }

    const bool hexDisplay = bool(el.type.flags & ShaderVariableFlags::HexDisplay);
    const bool binDisplay = bool(el.type.flags & ShaderVariableFlags::BinaryDisplay);

    if(hexDisplay && prop.format.type == ResourceFormatType::Regular)
      ret = Formatter::HexFormat(u, prop.format.compByteWidth);
    else if(binDisplay && prop.format.type == ResourceFormatType::Regular)
      ret = Formatter::BinFormat(u, prop.format.compByteWidth);
    else
      ret = Formatter::Format(u, hexDisplay);
  }
  else if(vt == QMetaType::Int || vt == QMetaType::Short || vt == QMetaType::SChar)
  {
    int32_t i = v.toInt();

    if(prop.floatCastWrong)
    {
      float f = (float)i;
      memcpy(&i, &f, sizeof(f));
    }

    if(i >= 0)
      ret = lit(" ") + Formatter::Format(i);
    else
      ret = Formatter::Format(i);
  }
  else if(vt == QMetaType::ULongLong)
  {
    const bool hexDisplay = bool(el.type.flags & ShaderVariableFlags::HexDisplay);
    const bool binDisplay = bool(el.type.flags & ShaderVariableFlags::BinaryDisplay);

    if(binDisplay)
      ret = Formatter::BinFormat((uint64_t)v.toULongLong(), 8);
    else
      ret = Formatter::Format((uint64_t)v.toULongLong(), hexDisplay);
  }
  else if(vt == QMetaType::LongLong)
  {
    int64_t i = v.toLongLong();
    if(i >= 0)
      ret = lit(" ") + Formatter::Format(i);
    else
      ret = Formatter::Format(i);
  }
  else
  {
    ret = v.toString();
  }

  return ret;
}

static uint32_t MeshIndex(const BufferConfiguration &config, uint32_t row)
{
  if(!config.indices || !config.indices->hasData())
    return row;
  const uint32_t index = CalcIndex(config.indices, row, config.baseVertex, config.primRestart);
  return config.primRestart && index == config.primRestart ? ~0U : index;
}

static QVariantList MeshValues(const BufferConfiguration &config, uint32_t index, int column)
{
  if(column < 0 || column >= config.columns.count() || index == ~0U)
    return {};
  const ShaderConstant &element = config.columns[column];
  const BufferElementProperties &prop = config.props[column];
  if(prop.perprimitive || prop.buffer >= config.buffers.size())
    return {};
  if(prop.perinstance)
    index = config.curInstance / qMax(1, prop.instancerate);
  const BufferData *buffer = config.buffers[prop.buffer];
  const uint64_t offset = uint64_t(buffer->stride) * index + element.byteOffset;
  if(offset >= buffer->size())
    return {};
  const byte *start = buffer->data() + offset;
  return GetVariants(prop.format, element, start, buffer->end());
}

class BufferItemModel : public QAbstractItemModel
{
public:
  BufferItemModel(RDTableView *v, bool vertexInput, bool mesh, QObject *parent)
      : QAbstractItemModel(parent)
  {
    vertexInputData = vertexInput;
    meshView = mesh;
    view = v;
    view->setModel(this);
  }
  void beginReset()
  {
    emit beginResetModel();
    config.reset();
  }
  void endReset(const BufferConfiguration &conf)
  {
    config = conf;
    cacheColumns();
    totalColumnCount = columnLookup.count() + reservedColumnCount();
    emit endResetModel();
  }
  QModelIndex index(int row, int column, const QModelIndex &parent = QModelIndex()) const override
  {
    if(row < 0 || row >= rowCount())
      return QModelIndex();

    return createIndex(row, column);
  }

  QModelIndex parent(const QModelIndex &index) const override { return QModelIndex(); }
  int rowCount(const QModelIndex &parent = QModelIndex()) const override
  {
    int ret = config.numRows;
    if(config.pagingOffset > 0)
      ret++;

    if(ret == 0)
    {
      if(!config.statusString.isEmpty())
        ret += config.statusString.count(QLatin1Char('\n')) + 1;
      if(config.noVertices)
        ret++;
      if(config.noInstances)
        ret++;
    }

    return ret;
  }
  int columnCount(const QModelIndex &parent = QModelIndex()) const override
  {
    return totalColumnCount;
  }
  Qt::ItemFlags flags(const QModelIndex &index) const override
  {
    if(!index.isValid())
      return 0;

    return QAbstractItemModel::flags(index);
  }

  QVariant headerData(int section, Qt::Orientation orientation, int role) const override
  {
    if(section < totalColumnCount && orientation == Qt::Horizontal)
    {
      if(role == Qt::DisplayRole || role == Qt::ToolTipRole || role == columnGroupRole)
      {
        if(section == 0)
        {
          return meshView ? lit("VTX") : lit("Element");
        }
        else if(section == 1 && meshView)
        {
          return lit("IDX");
        }
        else
        {
          const ShaderConstant &el = elementForColumn(section);

          if(el.type.columns == 1 || role == Qt::ToolTipRole || role == columnGroupRole)
            return el.name;

          QChar comps[] = {QLatin1Char('x'), QLatin1Char('y'), QLatin1Char('z'), QLatin1Char('w')};

          return QFormatStr("%1.%2").arg(el.name).arg(comps[componentForIndex(section)]);
        }
      }
    }

    return QVariant();
  }

  QVariant data(const QModelIndex &index, int role = Qt::DisplayRole) const override
  {
    if(index.isValid())
    {
      if(role == Qt::SizeHintRole)
      {
        QStyleOptionViewItem opt = view->viewOptions();
        opt.features |= QStyleOptionViewItem::HasDisplay;

        // pad these columns to allow for sufficiently wide data
        if(index.column() < reservedColumnCount())
          opt.text = lit("4294967295");
        else
          opt.text = data(index).toString();

        opt.text.replace(QLatin1Char('\n'), QChar::LineSeparator);

        opt.styleObject = NULL;

        QStyle *style = opt.widget ? opt.widget->style() : QApplication::style();
        return style->sizeFromContents(QStyle::CT_ItemViewItem, &opt, QSize(), opt.widget);
      }

      uint32_t row = index.row();
      int col = index.column();

      if(config.pagingOffset > 0)
      {
        if(row == 0)
        {
          if(role == Qt::DisplayRole)
            return lit("...");
          return QVariant();
        }
        row--;
      }

      if(role == columnGroupRole)
      {
        if(col < reservedColumnCount())
          return -1 - col;
        else
          return columnLookup[col - reservedColumnCount()];
      }

      if((role == Qt::BackgroundRole || role == Qt::ForegroundRole) && col >= reservedColumnCount())
      {
        if(meshView)
        {
          int elIdx = columnLookup[col - reservedColumnCount()];
          int compIdx = componentForIndex(col);

          float lightnessOn = qBound(0.25, view->palette().color(QPalette::Base).lightnessF(), 0.75);
          float lightnessOff = lightnessOn > 0.5f ? lightnessOn + 0.2f : lightnessOn - 0.2f;

          static float a = 0.55f;
          static float b = 0.8f;

          if(elIdx == positionEl)
          {
            QColor backCol;
            if(compIdx != 3 || !vertexInputData)
            {
              backCol = QColor::fromHslF(0.55f, 0.75f, lightnessOn);
            }
            else
            {
              backCol = QColor::fromHslF(0.55f, 0.75f, lightnessOff);
            }

            if(role == Qt::ForegroundRole)
              return QBrush(contrastingColor(backCol, view->palette().color(QPalette::Text)));

            return backCol;
          }
          else if(secondaryEnabled && elIdx == secondaryEl)
          {
            QColor backCol;
            if((secondaryElAlpha && compIdx == 3) || (!secondaryElAlpha && compIdx != 3))
            {
              backCol = QColor::fromHslF(0.33f, 0.75f, lightnessOn);
            }
            else
            {
              backCol = QColor::fromHslF(0.33f, 0.75f, lightnessOff);
            }

            if(role == Qt::ForegroundRole)
              return QBrush(contrastingColor(backCol, view->palette().color(QPalette::Text)));

            return backCol;
          }
        }
        else
        {
          const ShaderConstant &el = elementForColumn(col);
          const BufferElementProperties &prop = propForColumn(col);

          if((el.type.flags & ShaderVariableFlags::RGBDisplay) && prop.buffer < config.buffers.size())
          {
            const byte *data = config.buffers[prop.buffer]->data();
            const byte *end = config.buffers[prop.buffer]->end();

            data += config.buffers[prop.buffer]->stride * row;
            data += el.byteOffset;

            // only slightly wasteful, we need to fetch all variants together
            // since some formats are packed and can't be read individually
            QVariantList list = GetVariants(prop.format, el, data, end);

            if(!list.isEmpty())
            {
              QMetaType::Type vt = GetVariantMetatype(list[0]);

              QColor rgb;

              if(vt == QMetaType::Double)
              {
                double r = qBound(0.0, list[0].toDouble(), 1.0);
                double g = list.size() > 1 ? qBound(0.0, list[1].toDouble(), 1.0) : 0.0;
                double b = list.size() > 2 ? qBound(0.0, list[2].toDouble(), 1.0) : 0.0;

                rgb = QColor::fromRgbF(ConvertLinearToSRGB(float(r)), ConvertLinearToSRGB(float(g)),
                                       ConvertLinearToSRGB(float(b)));
              }
              else if(vt == QMetaType::Float)
              {
                float r = qBound(0.0f, list[0].toFloat(), 1.0f);
                float g = list.size() > 1 ? qBound(0.0f, list[1].toFloat(), 1.0f) : 0.0;
                float b = list.size() > 2 ? qBound(0.0f, list[2].toFloat(), 1.0f) : 0.0;

                rgb = QColor::fromRgbF(ConvertLinearToSRGB(float(r)), ConvertLinearToSRGB(float(g)),
                                       ConvertLinearToSRGB(float(b)));
              }
              else if(vt == QMetaType::UInt || vt == QMetaType::UShort || vt == QMetaType::UChar)
              {
                uint r = qBound(0U, list[0].toUInt(), 255U);
                uint g = list.size() > 1 ? qBound(0U, list[1].toUInt(), 255U) : 0.0;
                uint b = list.size() > 2 ? qBound(0U, list[2].toUInt(), 255U) : 0.0;

                // we leave this as assuming it's in sRGB space since most commonly this will be an
                // 8-bit texture being viewed as a buffer
                rgb = QColor::fromRgb(r, g, b);
              }
              else if(vt == QMetaType::Int || vt == QMetaType::Short || vt == QMetaType::SChar)
              {
                int r = qBound(0, list[0].toInt(), 255);
                int g = list.size() > 1 ? qBound(0, list[1].toInt(), 255) : 0.0;
                int b = list.size() > 2 ? qBound(0, list[2].toInt(), 255) : 0.0;

                rgb = QColor::fromRgb(r, g, b);
              }
              else
              {
                return QVariant();
              }

              if(role == Qt::BackgroundRole)
                return QBrush(rgb);
              else if(role == Qt::ForegroundRole)
                return QBrush(contrastingColor(rgb, QColor::fromRgb(0, 0, 0)));
            }
          }
        }
      }

      if(role == Qt::BackgroundRole && meshView && !config.meshletVertexPrefixCounts.empty())
      {
        auto it = std::upper_bound(config.meshletVertexPrefixCounts.begin(),
                                   config.meshletVertexPrefixCounts.end(), row);

        if(it != config.meshletVertexPrefixCounts.begin())
          it--;

        size_t meshletIdx = it - config.meshletVertexPrefixCounts.begin();

        return meshletIdx % 2 ? view->palette().color(QPalette::AlternateBase)
                              : view->palette().color(QPalette::Base);
      }

      if(role == Qt::DisplayRole)
      {
        if(config.numRows == 0 &&
           (config.noInstances || config.noVertices || !config.statusString.isEmpty()))
        {
          if(col < 2)
            return lit("---");

          if(col != 2)
            return QVariant();

          if(!config.statusString.isEmpty())
          {
            return config.statusString.split(QLatin1Char('\n'))[row];
          }
          else if(config.noVertices && config.noInstances)
          {
            if(row == 0)
              return lit("No Vertices");
            else
              return lit("No Instances");
          }
          else if(config.noVertices)
          {
            return lit("No Vertices");
          }
          else if(config.noInstances)
          {
            return lit("No Instances");
          }
        }

        if(config.unclampedNumRows > config.pagingOffset + config.numRows && row >= config.numRows - 2)
        {
          if(meshView)
          {
            if(col < 2 && row == config.numRows - 1)
              return QString::number(config.unclampedNumRows - 1);
          }
          else
          {
            if(col == 0 && row == config.numRows - 1)
              return QString::number(config.unclampedNumRows - 1);
          }

          return lit("...");
        }

        if(col >= 0 && col < totalColumnCount && row < config.numRows)
        {
          if(col == 0)
          {
            if(meshView && !config.meshletVertexPrefixCounts.empty())
            {
              auto it = std::upper_bound(config.meshletVertexPrefixCounts.begin(),
                                         config.meshletVertexPrefixCounts.end(), row);

              if(it != config.meshletVertexPrefixCounts.begin())
                it--;

              size_t meshletIdx = it - config.meshletVertexPrefixCounts.begin();

              return QFormatStr("%1[%2]")
                  .arg(meshletIdx + config.taskOrMeshletOffset)
                  .arg(row + config.pagingOffset - *it);
            }
            else
            {
              return row + config.pagingOffset;
            }
          }

          uint32_t idx = row;

          if(config.indices && config.indices->hasData())
          {
            idx = CalcIndex(config.indices, row, config.baseVertex, config.primRestart);

            if(config.primRestart && idx == config.primRestart)
              return col == 1 ? lit("--") : lit(" Restart");

            if(idx == ~0U)
              return outOfBounds();
          }

          if(col == 1 && meshView)
          {
            // if we have separate displayIndices, fetch that for display instead
            if(config.displayIndices && config.displayIndices->hasData())
              idx = CalcIndex(config.displayIndices, row, config.displayBaseVertex,
                              config.primRestart);

            if(idx == ~0U)
              return outOfBounds();

            return idx;
          }

          const ShaderConstant &el = elementForColumn(col);
          const BufferElementProperties &prop = propForColumn(col);

          if(useGenerics(col))
            return interpretGeneric(col, el, prop);

          uint32_t instIdx = 0;
          if(prop.instancerate > 0)
            instIdx = config.curInstance / prop.instancerate;

          if(prop.buffer < config.buffers.size())
          {
            const byte *data = config.buffers[prop.buffer]->data();
            const byte *end = config.buffers[prop.buffer]->end();

            if(prop.perprimitive)
            {
              uint32_t prim = row / RENDERDOC_NumVerticesPerPrimitive(config.topology);
              data += config.perPrimitiveOffset;
              data += config.perPrimitiveStride * prim;
            }
            else if(!prop.perinstance)
            {
              data += config.buffers[prop.buffer]->stride * idx;
            }
            else
            {
              data += config.buffers[prop.buffer]->stride * instIdx;
            }

            data += el.byteOffset;

            // only slightly wasteful, we need to fetch all variants together
            // since some formats are packed and can't be read individually
            QVariantList list = GetVariants(prop.format, el, data, end);

            int comp = componentForIndex(col);

            if(comp < list.count())
            {
              uint32_t rowdim = el.type.rows;
              uint32_t coldim = el.type.columns;

              if(rowdim == 1)
              {
                QVariant v = list[comp];

                if(el.type.pointerTypeID != ~0U)
                {
                  PointerVal ptr;
                  ptr.pointer = v.toULongLong();
                  ptr.pointerTypeID = el.type.pointerTypeID;
                  v = ToQStr(ptr);
                }

                RichResourceTextInitialise(v, getCaptureContext(view));

                if(RichResourceTextCheck(v))
                  return v;

                return interpretVariant(v, el, prop);
              }
              else
              {
                QString ret;

                for(uint32_t r = 0; r < rowdim; r++)
                {
                  if(r > 0)
                    ret += lit("\n");

                  ret += interpretVariant(list[r * coldim + comp], el, prop);
                }

                return ret;
              }
            }
          }

          return outOfBounds();
        }
      }
    }

    return QVariant();
  }

  void setPosColumn(int pos)
  {
    QVector<int> roles = {Qt::BackgroundRole, Qt::ForegroundRole};

    if(pos == -1)
      pos = config.guessPositionColumn();

    if(positionEl != pos)
    {
      if(positionEl >= 0)
        emit dataChanged(index(0, firstColumnForElement(positionEl)),
                         index(rowCount() - 1, lastColumnForElement(positionEl)), roles);

      if(pos >= 0)
        emit dataChanged(index(0, firstColumnForElement(pos)),
                         index(rowCount() - 1, lastColumnForElement(pos)), roles);
    }

    positionEl = pos;
  }

  int posColumn() { return positionEl; }
  QString posName() { return config.columnName(positionEl); }
  void setSecondaryColumn(int sec, bool secEnabled, bool secAlpha)
  {
    QVector<int> roles = {Qt::BackgroundRole, Qt::ForegroundRole};

    if(sec == -1)
      sec = config.guessSecondaryColumn();

    if(secondaryEl != sec || secondaryElAlpha != secAlpha || secondaryEnabled != secEnabled)
    {
      if(secondaryEl >= 0 && secondaryEl != sec)
        emit dataChanged(index(0, firstColumnForElement(secondaryEl)),
                         index(rowCount() - 1, lastColumnForElement(secondaryEl)), roles);

      if(sec >= 0)
        emit dataChanged(index(0, firstColumnForElement(sec)),
                         index(rowCount() - 1, lastColumnForElement(sec)), roles);
    }

    secondaryEl = sec;
    secondaryElAlpha = secAlpha;
    secondaryEnabled = secEnabled;
  }

  int secondaryColumn() { return secondaryEl; }
  bool secondaryAlpha() { return secondaryElAlpha; }
  QString secondaryName() { return config.columnName(secondaryEl); }
  int elementIndexForColumn(int col) const
  {
    if(col < reservedColumnCount())
      return -1;

    return columnLookup[col - reservedColumnCount()];
  }

  const ShaderConstant &elementForColumn(int col) const
  {
    if(col >= reservedColumnCount())
      col -= reservedColumnCount();
    return config.columns[columnLookup[col]];
  }

  const BufferElementProperties &propForColumn(int col) const
  {
    if(col >= reservedColumnCount())
      col -= reservedColumnCount();
    return config.props[columnLookup[col]];
  }

  bool useGenerics(int col) const
  {
    if(col >= reservedColumnCount())
      col -= reservedColumnCount();
    col = columnLookup[col];
    return col < config.genericsEnabled.size() && config.genericsEnabled[col];
  }

  const BufferConfiguration &getConfig() { return config; }

  uint32_t meshIndex(uint32_t row) const { return MeshIndex(config, row); }

  QVariantList meshValues(uint32_t row, int column) const
  {
    return MeshValues(config, meshIndex(row), column);
  }

private:
  // constant data over the item model's lifetime
  // The view that this model is for
  RDTableView *view = NULL;
  // Is this the vertex input stage
  bool vertexInputData = false;
  // are we configured for mesh viewing, or for raw buffer data
  bool meshView = true;

  // the mutable configuration of what we're displaying.
  BufferConfiguration config;

  // Internal cached data, generated by cacheColumns() from endReset().
  // Only accessible to main UI thread

  // maps from column number (0-based from data, so excluding VTX/IDX columns)
  // to the column element in the columns list, and lists its component.
  //
  // So a float4, float3, int set of columns would be:
  // { 0, 0, 0, 0, 1, 1, 1, 2 };
  // { 0, 1, 2, 3, 0, 1, 2, 0 };
  QVector<int> columnLookup;
  QVector<int> componentLookup;
  // the total number of columns including any reserved ones like VTX / IDX
  int totalColumnCount = 0;

  // which format element is selected as position data
  int positionEl = -1;
  // which format element is selected as secondary data
  int secondaryEl = -1;
  // is secondary data enabled
  bool secondaryEnabled = false;
  // are we using the alpha channel for secondary data
  bool secondaryElAlpha = false;

  int reservedColumnCount() const { return (meshView ? 2 : 1); }
  int componentForIndex(int col) const
  {
    if(col >= reservedColumnCount())
      col -= reservedColumnCount();
    return componentLookup[col];
  }
  int firstColumnForElement(int el) const
  {
    for(int i = 0; i < columnLookup.count(); i++)
    {
      if(columnLookup[i] == el)
        return reservedColumnCount() + i;
    }

    return 0;
  }

  int lastColumnForElement(int el) const
  {
    for(int i = columnLookup.count() - 1; i >= 0; i--)
    {
      if(columnLookup[i] == el)
        return reservedColumnCount() + i;
    }

    return columnCount() - 1;
  }

  void cacheColumns()
  {
    columnLookup.clear();
    columnLookup.reserve(config.columns.count() * 4);
    componentLookup.clear();
    componentLookup.reserve(config.columns.count() * 4);

    for(int i = 0; i < config.columns.count(); i++)
    {
      uint32_t columnCount = config.columns[i].type.columns;

      for(uint32_t c = 0; c < columnCount; c++)
      {
        columnLookup.push_back(i);
        componentLookup.push_back((int)c);
      }
    }
  }

  QString outOfBounds() const { return lit("---"); }
  QString interpretGeneric(int col, const ShaderConstant &el, const BufferElementProperties &prop) const
  {
    int comp = componentForIndex(col);

    if(col >= reservedColumnCount())
      col -= reservedColumnCount();
    col = columnLookup[col];

    if(col < config.generics.size())
    {
      if(prop.format.compType == CompType::Float)
      {
        return interpretVariant(QVariant(config.generics[col].floatValue[comp]), el, prop);
      }
      else if(prop.format.compType == CompType::SInt)
      {
        return interpretVariant(QVariant(config.generics[col].intValue[comp]), el, prop);
      }
      else if(prop.format.compType == CompType::UInt)
      {
        return interpretVariant(QVariant(config.generics[col].uintValue[comp]), el, prop);
      }
    }

    return outOfBounds();
  }
};

struct CachedElData
{
  const ShaderConstant *el = NULL;
  const BufferElementProperties *prop = NULL;

  const byte *data = NULL;
  const byte *end = NULL;

  size_t stride;
  int byteSize;
  uint32_t instIdx = 0;

  int numColumns = 0;

  QByteArray nulls;
};

struct PopulateBufferData
{
  int sequence;
  uint32_t eventId = 0;
  QString cbufferError;

  int inHoriz;
  int out1Horiz;
  int out2Horiz;

  int inVert;
  int out1Vert;
  int out2Vert;

  CBufferData cb;
  uint64_t bufferReadSize = 0;

  // {In, Out1, Out2} x {primary, secondary}
  QString highlightNames[6];

  bool meshDispatch = false;

  BufferConfiguration inConfig, out1Config, out2Config;
  MeshFormat postOut1, postOut2;
};

struct CalcBoundingBoxData
{
  uint32_t eventId;

  BufferConfiguration input[3];

  BBoxData output;
};

void CacheDataForIteration(QVector<CachedElData> &cache, const rdcarray<ShaderConstant> &columns,
                           const rdcarray<BufferElementProperties> &props,
                           const QList<BufferData *> buffers, uint32_t inst)
{
  cache.reserve(columns.count());

  for(int col = 0; col < columns.count(); col++)
  {
    const ShaderConstant &el = columns[col];
    const BufferElementProperties &prop = props[col];

    CachedElData d;

    d.el = &el;
    d.prop = &prop;

    d.byteSize = el.type.arrayByteStride;
    d.nulls = QByteArray(d.byteSize, '\0');
    d.numColumns = el.type.columns;

    if(prop.instancerate > 0)
      d.instIdx = inst / prop.instancerate;

    if(prop.buffer < buffers.size())
    {
      d.data = buffers[prop.buffer]->data();
      d.end = buffers[prop.buffer]->end();

      d.stride = buffers[prop.buffer]->stride;

      d.data += el.byteOffset;

      if(prop.perinstance)
        d.data += d.stride * d.instIdx;
    }

    if(prop.perprimitive)
      d.end = d.data;

    cache.push_back(d);
  }
}

static void ConfigureStatusColumn(rdcarray<ShaderConstant> &columns,
                                  rdcarray<BufferElementProperties> &props)
{
  ShaderConstant f;
  f.name = "STATUS";
  f.type.columns = 1;
  f.type.rows = 1;

  BufferElementProperties p;
  p.format.type = ResourceFormatType::Regular;
  p.format.compType = CompType::UInt;
  p.format.compCount = 1;
  p.format.compByteWidth = 4;

  columns.push_back(f);
  props.push_back(p);
}

static void ConfigureColumnsForShader(ICaptureContext &ctx, int32_t streamSelect,
                                      const ShaderReflection *shader,
                                      rdcarray<ShaderConstant> &columns,
                                      rdcarray<BufferElementProperties> &props)
{
  if(!shader)
    return;

  columns.reserve(shader->outputSignature.count());
  props.reserve(shader->outputSignature.count());

  int i = 0, posidx = -1;
  for(const SigParameter &sig : shader->outputSignature)
  {
    if(sig.stream != (uint32_t)streamSelect)
      continue;

    if(sig.systemValue == ShaderBuiltin::OutputIndices)
      continue;

    ShaderConstant f;
    BufferElementProperties p;

    f.name = !sig.varName.isEmpty() ? sig.varName : sig.semanticIdxName;
    if(sig.perPrimitiveRate)
      f.name += lit(" (Per-Prim)");
    f.type.rows = 1;
    f.type.columns = sig.compCount;

    p.buffer = 0;
    p.perinstance = false;
    p.perprimitive = sig.perPrimitiveRate;
    p.instancerate = 1;
    p.systemValue = sig.systemValue;
    p.format.type = ResourceFormatType::Regular;
    p.format.compByteWidth = qMax<uint32_t>(sizeof(float), VarTypeByteSize(sig.varType));
    p.format.compCount = sig.compCount;
    p.format.compType = VarTypeCompType(sig.varType);

    f.type.arrayByteStride = p.format.compByteWidth * p.format.compCount;

    if(sig.systemValue == ShaderBuiltin::Position)
      posidx = i;

    columns.push_back(f);
    props.push_back(p);

    i++;
  }

  // shift position attribute up to first, keeping order otherwise
  // the same
  if(posidx > 0)
  {
    columns.insert(0, columns.takeAt(posidx));
    props.insert(0, props.takeAt(posidx));
  }

  i = 0;
  uint32_t perPrimOffset = 0, perVertOffset = 0;
  for(i = 0; i < columns.count(); i++)
  {
    BufferElementProperties &prop = props[i];
    ShaderConstant &el = columns[i];

    uint numComps = el.type.columns;
    uint elemSize = prop.format.compByteWidth > 4 ? 8U : 4U;

    MeshDataStage outStage = MeshDataStage::VSOut;

    switch(shader->stage)
    {
      case ShaderStage::Vertex: outStage = MeshDataStage::VSOut; break;
      case ShaderStage::Hull: outStage = MeshDataStage::GSOut; break;
      case ShaderStage::Domain: outStage = MeshDataStage::GSOut; break;
      case ShaderStage::Geometry: outStage = MeshDataStage::GSOut; break;
      case ShaderStage::Task: outStage = MeshDataStage::TaskOut; break;
      case ShaderStage::Mesh: outStage = MeshDataStage::MeshOut; break;
      default: break;
    }

    uint32_t &offset = prop.perprimitive ? perPrimOffset : perVertOffset;

    if(ctx.CurPipelineState().HasAlignedPostVSData(outStage))
    {
      if(numComps == 2)
        offset = AlignUp(offset, 2U * elemSize);
      else if(numComps > 2)
        offset = AlignUp(offset, 4U * elemSize);
    }

    el.byteOffset = offset;

    offset += numComps * elemSize;
  }
}

static void ConfigureColumnsForMeshPipe(ICaptureContext &ctx, PopulateBufferData *bufdata)
{
  bufdata->inConfig.statusString = lit("No input visualisation supported for mesh shaders");

  ConfigureStatusColumn(bufdata->inConfig.columns, bufdata->inConfig.props);

  const ShaderReflection *ts = ctx.CurPipelineState().GetShaderReflection(ShaderStage::Task);

  if(ts && bufdata->out1Config.statusString.isEmpty())
  {
    bufdata->out1Config.columns = ts->taskPayload.variables;
    bufdata->out1Config.props.resize(bufdata->out1Config.columns.size());
  }
  else
  {
    if(bufdata->out1Config.statusString.isEmpty())
      bufdata->out1Config.statusString = lit("No output visualisation supported for task shaders");
    ConfigureStatusColumn(bufdata->out1Config.columns, bufdata->out1Config.props);
  }

  const ShaderReflection *ms = ctx.CurPipelineState().GetShaderReflection(ShaderStage::Mesh);

  ConfigureColumnsForShader(ctx, 0, ms, bufdata->out2Config.columns, bufdata->out2Config.props);
}

static void ConfigureColumnsForVertexPipe(ICaptureContext &ctx, PopulateBufferData *bufdata)
{
  const ActionDescription *action = ctx.CurAction();

  bufdata->inConfig.numRows = 0;
  bufdata->inConfig.unclampedNumRows = 0;

  bufdata->inConfig.noVertices = false;
  bufdata->inConfig.noInstances = false;

  rdcarray<VertexInputAttribute> vinputs = ctx.CurPipelineState().GetVertexInputs();

  bufdata->inConfig.columns.reserve(vinputs.count());
  bufdata->inConfig.columns.clear();
  bufdata->inConfig.props.reserve(vinputs.count());
  bufdata->inConfig.props.clear();
  bufdata->inConfig.genericsEnabled.resize(vinputs.count());
  bufdata->inConfig.generics.resize(vinputs.count());

  for(const VertexInputAttribute &a : vinputs)
  {
    if(!a.used)
      continue;

    ShaderConstant f;
    f.name = a.name;
    f.byteOffset = a.byteOffset;
    f.type.columns = a.format.compCount;
    f.type.rows = 1;
    f.type.arrayByteStride = f.type.matrixByteStride = a.format.ElementSize();

    BufferElementProperties p;
    p.buffer = a.vertexBuffer;
    p.perinstance = a.perInstance;
    p.instancerate = a.instanceRate;
    p.floatCastWrong = a.floatCastWrong;
    p.format = a.format;

    bufdata->inConfig.genericsEnabled[bufdata->inConfig.columns.count()] = false;

    if(a.genericEnabled)
    {
      bufdata->inConfig.genericsEnabled[bufdata->inConfig.columns.count()] = true;
      bufdata->inConfig.generics[bufdata->inConfig.columns.count()] = a.genericValue;
    }

    bufdata->inConfig.columns.push_back(f);
    bufdata->inConfig.props.push_back(p);
  }

  bufdata->inConfig.numRows = action->numIndices;
  bufdata->inConfig.unclampedNumRows = 0;

  // calculate an upper bound on the valid number of rows just in case it's an invalid value (e.g.
  // 0xdeadbeef) and we want to clamp.
  uint32_t numRowsUpperBound = 0;

  if(action->flags & ActionFlags::Indexed)
  {
    // In an indexed draw we clamp to however many indices are available in the index buffer

    BoundVBuffer ib = ctx.CurPipelineState().GetIBuffer();

    uint32_t bytesAvailable = ib.byteSize;

    if(bytesAvailable == ~0U)
    {
      BufferDescription *buf = ctx.GetBuffer(ib.resourceId);
      if(buf)
      {
        uint64_t offset = ib.byteOffset + action->indexOffset * ib.byteStride;
        if(offset > buf->length)
          bytesAvailable = 0;
        else
          bytesAvailable = buf->length - offset;
      }
      else
      {
        bytesAvailable = 0;
      }
    }

    // drawing more than this many indices will read off the end of the index buffer - which while
    // technically not invalid is certainly not intended, so serves as a good 'upper bound'
    numRowsUpperBound = bytesAvailable / qMax(1U, ib.byteStride);
  }
  else
  {
    // for a non-indexed draw, we take the largest vertex buffer
    rdcarray<BoundVBuffer> VBs = ctx.CurPipelineState().GetVBuffers();

    for(const BoundVBuffer &vb : VBs)
    {
      if(vb.byteStride == 0)
        continue;

      uint32_t bytesAvailable = vb.byteSize;

      if(bytesAvailable == ~0U)
      {
        BufferDescription *buf = ctx.GetBuffer(vb.resourceId);
        if(buf)
        {
          if(vb.byteOffset > buf->length)
            bytesAvailable = 0;
          else
            bytesAvailable = buf->length - vb.byteOffset;
        }
        else
        {
          bytesAvailable = 0;
        }
      }

      numRowsUpperBound = qMax(numRowsUpperBound, bytesAvailable / qMax(1U, vb.byteStride));
    }

    // if there are no vertex buffers we can't clamp.
    if(numRowsUpperBound == 0)
      numRowsUpperBound = ~0U;
  }

  // if we have significantly clamped, then set the unclamped number of rows and clamp.
  if(numRowsUpperBound != ~0U && numRowsUpperBound + 100 < bufdata->inConfig.numRows)
  {
    bufdata->inConfig.unclampedNumRows = bufdata->inConfig.numRows;
    bufdata->inConfig.numRows = numRowsUpperBound + 100;
  }

  if((action->flags & ActionFlags::Drawcall) && action->numIndices == 0)
    bufdata->inConfig.noVertices = true;

  if((action->flags & ActionFlags::Instanced) && action->numInstances == 0)
  {
    bufdata->inConfig.noInstances = true;
    bufdata->inConfig.numRows = bufdata->inConfig.unclampedNumRows = 0;
  }

  bufdata->out1Config.columns.clear();
  bufdata->out1Config.props.clear();
  bufdata->out2Config.columns.clear();
  bufdata->out2Config.props.clear();

  const ShaderReflection *vs = ctx.CurPipelineState().GetShaderReflection(ShaderStage::Vertex);
  const ShaderReflection *last = ctx.CurPipelineState().GetShaderReflection(ShaderStage::Geometry);
  if(last == NULL)
    last = ctx.CurPipelineState().GetShaderReflection(ShaderStage::Domain);

  ConfigureColumnsForShader(ctx, 0, vs, bufdata->out1Config.columns, bufdata->out1Config.props);
  ConfigureColumnsForShader(ctx, ctx.CurPipelineState().GetRasterizedStream(), last,
                            bufdata->out2Config.columns, bufdata->out2Config.props);
}

static void ConfigureColumns(ICaptureContext &ctx, PopulateBufferData *bufdata)
{
  const ActionDescription *action = ctx.CurAction();

  if(action && (action->flags & ActionFlags::MeshDispatch))
  {
    ConfigureColumnsForMeshPipe(ctx, bufdata);
  }
  else if(action && (action->flags & ActionFlags::Drawcall))
  {
    ConfigureColumnsForVertexPipe(ctx, bufdata);
  }
  else
  {
    IEventBrowser *eb = ctx.GetEventBrowser();

    bufdata->inConfig.statusString = bufdata->out1Config.statusString =
        bufdata->out2Config.statusString =
            lit("No current draw action\nSelected EID @%1 - %2\nEffective EID: @%3 - %4")
                .arg(ctx.CurSelectedEvent())
                .arg(QString(eb->GetEventName(ctx.CurSelectedEvent())))
                .arg(ctx.CurEvent())
                .arg(QString(eb->GetEventName(ctx.CurEvent())));

    ConfigureStatusColumn(bufdata->inConfig.columns, bufdata->inConfig.props);
    ConfigureStatusColumn(bufdata->out1Config.columns, bufdata->out1Config.props);
    ConfigureStatusColumn(bufdata->out2Config.columns, bufdata->out2Config.props);

    bufdata->inConfig.genericsEnabled.push_back(false);
    bufdata->inConfig.generics.push_back(PixelValue());
  }
}

static void ApplyMeshChannelAliases(IReplayController *controller, const PipeState &pipe,
                                    ShaderStage stage, bool inputs, BufferConfiguration &config)
{
  const ShaderReflection *original = pipe.GetShaderReflection(stage);
  if(!original || config.columns.empty())
    return;
  const ResourceId resource = pipe.GetShader(stage);
  const ResourceId target = controller->GetTargetShaderReplacement(resource);
  if(target == ResourceId() || target == resource)
    return;
  ShaderEntryPoint entry;
  entry.name = pipe.GetShaderEntryPoint(stage);
  entry.stage = stage;
  const ShaderReflection *edited = controller->GetShader(ResourceId(), target, entry);
  if(!edited)
    return;
  const auto &captured = inputs ? original->inputSignature : original->outputSignature;
  const auto &effective = inputs ? edited->inputSignature : edited->outputSignature;
  for(int column = 0; column < config.columns.count(); ++column)
  {
    const auto &element = config.columns[column];
    const SigParameter *source = NULL;
    for(const SigParameter &signature : captured)
    {
      if(signature.compCount != element.type.columns ||
         (signature.varName != element.name && signature.semanticIdxName != element.name))
        continue;
      if(source)
      {
        source = NULL;
        break;
      }
      source = &signature;
    }
    if(!source || source->systemValue != ShaderBuiltin::Undefined)
      continue;
    const SigParameter *replacement = NULL;
    for(const SigParameter &signature : effective)
    {
      if(signature.regIndex != source->regIndex ||
         signature.regChannelMask != source->regChannelMask ||
         signature.systemValue != source->systemValue || signature.varType != source->varType ||
         signature.stream != source->stream || signature.compCount != source->compCount ||
         signature.perPrimitiveRate != source->perPrimitiveRate)
        continue;
      if(replacement)
      {
        replacement = NULL;
        break;
      }
      replacement = &signature;
    }
    if(replacement && !replacement->varName.empty())
      config.channelAliases[column] = QString::fromUtf8(replacement->varName.c_str());
  }
}

static void RT_FetchMeshPipeData(IReplayController *r, ICaptureContext &ctx, PopulateBufferData *data)
{
  uint32_t numIndices = data->postOut2.numIndices;

  if(data->inConfig.indices)
    data->inConfig.indices->deref();

  data->inConfig.indices = NULL;

  data->out1Config.numRows = data->postOut1.numIndices;
  data->out1Config.unclampedNumRows = 0;

  if(data->out1Config.indices)
    data->out1Config.indices->deref();
  if(data->out1Config.displayIndices)
    data->out1Config.displayIndices->deref();
  data->out1Config.displayIndices = NULL;

  data->out1Config.dispatchSize = data->postOut1.dispatchSize;
  data->out1Config.taskSizes = data->postOut1.taskSizes;

  if(data->postOut1.vertexResourceId != ResourceId())
  {
    BufferData *postts = new BufferData;
    postts->storage =
        r->GetBufferData(data->postOut1.vertexResourceId, data->postOut1.vertexByteOffset, 0);

    postts->stride = data->postOut1.vertexByteStride;

    // ref passes to model
    data->out1Config.buffers.push_back(postts);
  }

  data->out1Config.statusString = data->postOut1.status;

  if(data->out2Config.indices)
    data->out2Config.indices->deref();
  if(data->out2Config.displayIndices)
    data->out2Config.displayIndices->deref();
  data->out2Config.displayIndices = NULL;

  uint32_t count = 0;
  for(const MeshletSize &meshletSize : data->postOut2.meshletSizes)
  {
    data->out2Config.meshletVertexPrefixCounts.push_back(count);
    count += meshletSize.numIndices;
  }

  data->out2Config.numRows = numIndices;
  data->out2Config.unclampedNumRows = 0;

  data->out2Config.topology = data->postOut2.topology;
  data->out2Config.perPrimitiveOffset = data->postOut2.perPrimitiveOffset;
  data->out2Config.perPrimitiveStride = data->postOut2.perPrimitiveStride;

  bytebuf idata = r->GetBufferData(data->postOut2.indexResourceId, data->postOut2.indexByteOffset,
                                   numIndices * data->postOut2.indexByteStride);

  data->out2Config.indices = new BufferData();
  data->out2Config.indices->storage.resize(sizeof(uint32_t) * numIndices);
  uint32_t *indices = (uint32_t *)data->out2Config.indices->data();

  memcpy(indices, idata.data(), qMin(idata.size(), numIndices * sizeof(uint32_t)));

  if(data->postOut2.vertexResourceId != ResourceId())
  {
    BufferData *postms = new BufferData;
    postms->storage =
        r->GetBufferData(data->postOut2.vertexResourceId, data->postOut2.vertexByteOffset, 0);

    postms->stride = data->postOut2.vertexByteStride;

    // ref passes to model
    data->out2Config.buffers.push_back(postms);
  }

  data->out2Config.perPrimitiveOffset = data->postOut2.perPrimitiveOffset;
  data->out2Config.perPrimitiveStride = data->postOut2.perPrimitiveStride;

  data->out2Config.statusString = data->postOut2.status;
}

static void RT_FetchVertexPipeData(IReplayController *r, ICaptureContext &ctx,
                                   PopulateBufferData *data)
{
  const ActionDescription *action = ctx.CurAction();

  BoundVBuffer ib = ctx.CurPipelineState().GetIBuffer();

  rdcarray<BoundVBuffer> vbs = ctx.CurPipelineState().GetVBuffers();

  uint32_t numIndices = action ? action->numIndices : 0;

  bytebuf idata;
  if(ib.resourceId != ResourceId() && action && (action->flags & ActionFlags::Indexed))
  {
    uint64_t readBytes = numIndices * ib.byteStride;
    uint32_t offset = action->indexOffset * ib.byteStride;

    if(ib.byteSize > offset)
      readBytes = qMin(ib.byteSize - offset, readBytes);
    else
      readBytes = 0;

    if(readBytes > 0)
      idata = r->GetBufferData(ib.resourceId, ib.byteOffset + offset, readBytes);
  }

  if(data->inConfig.indices)
    data->inConfig.indices->deref();

  data->inConfig.indices = new BufferData();

  if(action && ib.byteStride != 0 && !idata.isEmpty())
    data->inConfig.indices->storage.resize(
        sizeof(uint32_t) *
        qMin(numIndices, (((uint32_t)idata.size() + ib.byteStride - 1) / ib.byteStride)));
  else if(action && (action->flags & ActionFlags::Indexed))
    data->inConfig.indices->storage.resize(sizeof(uint32_t));

  uint32_t *indices = (uint32_t *)data->inConfig.indices->data();

  uint32_t maxIndex = 0;
  if(action)
    maxIndex = qMax(1U, numIndices) - 1;

  if(action && !idata.isEmpty())
  {
    maxIndex = 0;
    if(ib.byteStride == 1)
    {
      uint8_t primRestart = data->inConfig.primRestart & 0xff;

      for(size_t i = 0; i < idata.size() && (uint32_t)i < numIndices; i++)
      {
        indices[i] = (uint32_t)idata[i];
        if(primRestart && indices[i] == primRestart)
          continue;

        maxIndex = qMax(maxIndex, indices[i]);
      }
    }
    else if(ib.byteStride == 2)
    {
      uint16_t primRestart = data->inConfig.primRestart & 0xffff;

      uint16_t *src = (uint16_t *)idata.data();
      for(size_t i = 0; i < idata.size() / sizeof(uint16_t) && (uint32_t)i < numIndices; i++)
      {
        indices[i] = (uint32_t)src[i];
        if(primRestart && indices[i] == primRestart)
          continue;

        maxIndex = qMax(maxIndex, indices[i]);
      }
    }
    else if(ib.byteStride == 4)
    {
      uint32_t primRestart = data->inConfig.primRestart;

      memcpy(indices, idata.data(), qMin(idata.size(), numIndices * sizeof(uint32_t)));

      for(uint32_t i = 0; i < idata.size() / sizeof(uint32_t) && i < numIndices; i++)
      {
        if(primRestart && indices[i] == primRestart)
          continue;

        maxIndex = qMax(maxIndex, indices[i]);
      }
    }
  }

  int vbIdx = 0;
  for(BoundVBuffer vb : vbs)
  {
    bool used = false;
    bool pi = false;
    bool pv = false;

    uint32_t maxAttrOffset = 0;

    for(int c = 0; c < data->inConfig.columns.count(); c++)
    {
      const ShaderConstant &col = data->inConfig.columns[c];
      const BufferElementProperties &prop = data->inConfig.props[c];

      if(prop.buffer == vbIdx)
      {
        used = true;

        maxAttrOffset = qMax(maxAttrOffset, col.byteOffset);

        if(prop.perinstance)
          pi = true;
        else
          pv = true;
      }
    }

    vbIdx++;

    uint32_t maxIdx = 0;
    uint32_t offset = 0;

    if(used && action)
    {
      if(pi)
      {
        maxIdx = qMax(1U, action->numInstances) - 1;
        offset = action->instanceOffset;
      }
      if(pv)
      {
        maxIdx = qMax(maxIndex, maxIdx);
        offset = action->vertexOffset;

        if(action->baseVertex > 0)
          maxIdx = qMax(maxIdx, maxIdx + (uint32_t)action->baseVertex);
      }

      if(pi && pv)
        qCritical() << "Buffer used for both instance and vertex rendering!";
    }

    BufferData *buf = new BufferData;
    if(used)
    {
      uint64_t readBytes = qMax(maxIdx, maxIdx + 1) * vb.byteStride + maxAttrOffset;

      // if the stride is 0, allow reading at most one float4. This will still get clamped by the
      // declared vertex buffer size below
      if(vb.byteStride == 0)
        readBytes += 16;

      offset *= vb.byteStride;

      if(vb.byteSize > offset)
        readBytes = qMin(vb.byteSize - offset, readBytes);
      else
        readBytes = 0;

      if(readBytes > 0)
        buf->storage = r->GetBufferData(vb.resourceId, vb.byteOffset + offset, readBytes);

      buf->stride = vb.byteStride;
    }
    // ref passes to model
    data->inConfig.buffers.push_back(buf);
  }

  if(data->postOut1.numIndices <= data->inConfig.numRows)
  {
    data->out1Config.numRows = data->postOut1.numIndices;
    data->out1Config.unclampedNumRows = 0;
  }
  else
  {
    // the vertex shader can't run any expansion, so apply the same clamping to it as we applied to
    // the inputs. This protects against draws with an invalid number of vertices.
    data->out1Config.numRows = data->inConfig.numRows;
    data->out1Config.unclampedNumRows = data->inConfig.unclampedNumRows;
  }

  data->out1Config.statusString = data->postOut1.status;

  data->out1Config.baseVertex = data->postOut1.baseVertex;
  data->out1Config.displayBaseVertex = data->inConfig.baseVertex;

  if(action && data->postOut1.indexResourceId != ResourceId() &&
     (action->flags & ActionFlags::Indexed))
    idata = r->GetBufferData(data->postOut1.indexResourceId, data->postOut1.indexByteOffset,
                             numIndices * data->postOut1.indexByteStride);

  indices = NULL;
  if(data->out1Config.indices)
    data->out1Config.indices->deref();
  if(data->out1Config.displayIndices)
    data->out1Config.displayIndices->deref();

  {
    // display the same index values
    data->out1Config.displayIndices = data->inConfig.indices;
    data->out1Config.displayIndices->ref();

    data->out1Config.indices = new BufferData();
    if(action && ib.byteStride != 0 && !idata.isEmpty())
    {
      data->out1Config.indices->storage.resize(sizeof(uint32_t) * numIndices);
      indices = (uint32_t *)data->out1Config.indices->data();

      if(ib.byteStride == 1)
      {
        for(size_t i = 0; i < idata.size() && (uint32_t)i < numIndices; i++)
          indices[i] = (uint32_t)idata[i];
      }
      else if(ib.byteStride == 2)
      {
        uint16_t *src = (uint16_t *)idata.data();
        for(size_t i = 0; i < idata.size() / sizeof(uint16_t) && (uint32_t)i < numIndices; i++)
          indices[i] = (uint32_t)src[i];
      }
      else if(ib.byteStride == 4)
      {
        memcpy(indices, idata.data(), qMin(idata.size(), numIndices * sizeof(uint32_t)));
      }
    }
  }

  if(data->postOut1.vertexResourceId != ResourceId())
  {
    BufferData *postvs = new BufferData;
    postvs->storage =
        r->GetBufferData(data->postOut1.vertexResourceId, data->postOut1.vertexByteOffset, 0);

    postvs->stride = data->postOut1.vertexByteStride;

    // ref passes to model
    data->out1Config.buffers.push_back(postvs);
  }

  data->out2Config.statusString = data->postOut2.status;

  data->out2Config.numRows = data->postOut2.numIndices;
  data->out2Config.unclampedNumRows = 0;
  data->out2Config.baseVertex = data->postOut2.baseVertex;
  data->out2Config.displayBaseVertex = data->inConfig.baseVertex;

  indices = NULL;
  data->out2Config.indices = NULL;

  if(data->postOut2.vertexResourceId != ResourceId())
  {
    BufferData *postgs = new BufferData;
    postgs->storage =
        r->GetBufferData(data->postOut2.vertexResourceId, data->postOut2.vertexByteOffset, 0);

    postgs->stride = data->postOut2.vertexByteStride;

    // ref passes to model
    data->out2Config.buffers.push_back(postgs);
  }
}

static int MaxNumRows(const ShaderConstant &c)
{
  int ret = c.type.rows;

  if(c.type.baseType != VarType::Enum)
  {
    for(const ShaderConstant &child : c.type.members)
      ret = qMax(ret, MaxNumRows(child));
  }

  return ret;
}

static void UnrollConstant(rdcstr prefix, uint32_t baseOffset, const ShaderConstant &constant,
                           rdcarray<ShaderConstant> &columns,
                           rdcarray<BufferElementProperties> &props)
{
  bool isArray = constant.type.elements > 1;

  rdcstr baseName = constant.name;

  if(!prefix.isEmpty())
    baseName = prefix + "." + baseName;

  if(constant.type.baseType == VarType::Enum || constant.type.members.isEmpty())
  {
    BufferElementProperties prop;
    prop.format = GetInterpretedResourceFormat(constant);

    ShaderConstant c = constant;
    c.byteOffset += baseOffset;

    if(isArray)
    {
      for(uint32_t a = 0; a < constant.type.elements; a++)
      {
        c.name = QFormatStr("%1[%2]").arg(baseName).arg(a);
        columns.push_back(c);
        props.push_back(prop);
        c.byteOffset += constant.type.arrayByteStride;
      }
    }
    else
    {
      c.name = baseName;
      columns.push_back(c);
      props.push_back(prop);
    }

    return;
  }

  // struct, expand by members
  uint32_t arraySize = qMax(1U, constant.type.elements);
  if(arraySize == ~0U)
    arraySize = 1U;
  for(uint32_t a = 0; a < arraySize; a++)
  {
    for(const ShaderConstant &child : constant.type.members)
    {
      UnrollConstant(isArray ? QFormatStr("%1[%2]").arg(baseName).arg(a) : QString(baseName),
                     baseOffset + constant.byteOffset + a * constant.type.arrayByteStride, child,
                     columns, props);
    }
  }
}

static void UnrollConstant(const ShaderConstant &constant, rdcarray<ShaderConstant> &columns,
                           rdcarray<BufferElementProperties> &props)
{
  UnrollConstant("", 0, constant, columns, props);
}

QList<BufferViewer *> BufferViewer::m_CBufferViews;

BufferViewer::BufferViewer(ICaptureContext &ctx, bool meshview, QWidget *parent)
    : QFrame(parent), ui(new Ui::BufferViewer), m_Ctx(ctx)
{
  ui->setupUi(this);

  if(!meshview)
  {
    m_CBufferAliasWatcher = new QFileSystemWatcher(this);
    m_CBufferAliasRefreshTimer = new QTimer(this);
    m_CBufferAliasRefreshTimer->setSingleShot(true);
    m_CBufferAliasRefreshTimer->setInterval(150);
    QObject::connect(m_CBufferAliasRefreshTimer, &QTimer::timeout, this,
                     &BufferViewer::UI_RefreshCBufferAliasDisplay);
    QObject::connect(m_CBufferAliasWatcher, &QFileSystemWatcher::fileChanged, this,
                     [this](const QString &path) {
                       if(path == m_CBufferAliasFile)
                         m_CBufferAliasRefreshTimer->start();
                     });
    QObject::connect(m_CBufferAliasWatcher, &QFileSystemWatcher::directoryChanged, this,
                     [this](const QString &) {
                       // The source alias directory may be created by a later asynchronous
                       // publication. Follow it from the nearest existing parent without
                       // changing the displayed model's sequence or acknowledging its stamp.
                       if(!m_CBufferAliasFile.isEmpty())
                       {
                         QString directory = QFileInfo(m_CBufferAliasFile).absolutePath();
                         while(!QDir(directory).exists())
                         {
                           const QString parent = QFileInfo(directory).absolutePath();
                           if(parent == directory)
                             break;
                           directory = parent;
                         }
                         if(!m_CBufferAliasWatcher->directories().contains(directory))
                           m_CBufferAliasWatcher->addPath(directory);
                       }
                       // A project contains many other layouts and a publication lock. Ignore
                       // their changes without reading any JSON or querying the replay thread.
                       if(CBufferAliasFileStamp(m_CBufferAliasFile) != m_CBufferAliasFileStamp)
                         m_CBufferAliasRefreshTimer->start();
                     });
  }

  ui->render->SetContext(m_Ctx);

  byteRangeStart = (RDSpinBox64 *)ui->byteRangeStart;
  byteRangeLength = (RDSpinBox64 *)ui->byteRangeLength;

  byteRangeStart->configure();
  byteRangeLength->configure();

  byteRangeStart->setMinimum(0ULL);
  byteRangeLength->setMinimum(0ULL);

  m_ModelIn = new BufferItemModel(ui->inTable, true, meshview, this);
  m_ModelOut1 = new BufferItemModel(ui->out1Table, false, meshview, this);
  m_ModelOut2 = new BufferItemModel(ui->out2Table, false, meshview, this);

  if(meshview)
  {
    ui->inTable->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOn);
    ui->inTable->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOn);
    ui->out1Table->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOn);
    ui->out1Table->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOn);
    ui->out2Table->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOn);
    ui->out2Table->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOn);
  }

  m_MeshDebugSelector = new ComputeDebugSelector(this);

  // we keep the old UI names for serialised layouts compatibility
  QString containerNames[] = {
      lit("vsinData"),
      lit("vsoutData"),
      lit("gsoutData"),
  };

  for(size_t i = 0; i < 3; i++)
  {
    m_Containers[i] = new QWidget(this);
    // for layout compatibility
    m_Containers[i]->setObjectName(containerNames[i]);

    QVBoxLayout *layout = new QVBoxLayout(m_Containers[i]);
    layout->setSpacing(0);
    layout->setContentsMargins(0, 0, 0, 0);
  }

  if(meshview)
  {
    m_Containers[0]->layout()->addWidget(ui->inTable);
    m_Containers[0]->layout()->addWidget(ui->fixedVars);
    m_Containers[1]->layout()->addWidget(ui->out1Table);
    m_Containers[2]->layout()->addWidget(ui->out2Table);

    ui->fixedVars->setVisible(false);
  }

  m_MeshView = meshview;

  ui->formatSpecifier->setContext(&m_Ctx);

  m_Flycam = new FlycamWrapper(m_Ctx);
  m_Arcball = new ArcballWrapper(m_Ctx);
  m_CurrentCamera = m_Arcball;

  m_Output = NULL;

  m_Config = MeshDisplay();
  m_Config.type = MeshDataStage::VSIn;
  m_Config.wireframeDraw = true;
  m_Config.exploderScale = 1.0f;

  ui->outputTabs->setCurrentIndex(0);
  m_CurStage = MeshDataStage::VSIn;

  ui->inTable->setFont(Formatter::FixedFont());
  ui->out1Table->setFont(Formatter::FixedFont());
  ui->out2Table->setFont(Formatter::FixedFont());

  ui->minBoundsLabel->setFont(Formatter::FixedFont());
  ui->maxBoundsLabel->setFont(Formatter::FixedFont());

  ui->rowOffset->setFont(Formatter::PreferredFont());
  ui->instance->setFont(Formatter::PreferredFont());
  ui->viewIndex->setFont(Formatter::PreferredFont());
  ui->camSpeed->setFont(Formatter::PreferredFont());

  if(meshview)
  {
    SetupMeshView();
    if(isMeshDraw())
    {
      m_CurStage = MeshDataStage::TaskOut;
      m_Config.type = MeshDataStage::TaskOut;
    }
  }
  else
    SetupRawView();

  m_ExportMenu = new QMenu(this);
  m_ExportCSV = new QAction(this);
  m_ExportCSV->setIcon(Icons::save());
  m_ExportBytes = new QAction(this);
  m_ExportBytes->setIcon(Icons::save());

  m_ExportMenu->addAction(m_ExportCSV);
  m_ExportMenu->addAction(m_ExportBytes);

  m_DebugVert = new QAction(tr("&Debug this Vertex"), this);
  m_DebugVert->setIcon(Icons::wrench());

  m_CopyCSV = new QAction(tr("&Copy as CSV"), this);
  m_CopyCSV->setIcon(Icons::copy());

  m_DebugMeshThread = new QAction(tr("&Debug Mesh Thread"), this);
  m_DebugMeshThread->setIcon(Icons::wrench());

  m_FilterMesh = new QAction(tr("&Filter to this Meshlet"), this);
  m_FilterMesh->setIcon(Icons::filter());

  m_RemoveFilter = new QAction(tr("&Remove Filter"), this);
  m_RemoveFilter->setIcon(Icons::arrow_undo());

  m_GotoTask = new QAction(tr("&Go to task"), this);
  m_GotoTask->setIcon(Icons::arrow_join());

  ui->exportDrop->setMenu(m_ExportMenu);

  QObject::connect(m_ExportMenu, &QMenu::aboutToShow, this, &BufferViewer::updateExportActionNames);

  QObject::connect(m_ExportCSV, &QAction::triggered,
                   [this] { exportData(BufferExport(BufferExport::CSV)); });
  QObject::connect(m_ExportBytes, &QAction::triggered,
                   [this] { exportData(BufferExport(BufferExport::RawBytes)); });
  QObject::connect(m_DebugVert, &QAction::triggered, this, &BufferViewer::debugVertex);
  QObject::connect(m_DebugMeshThread, &QAction::triggered, this, &BufferViewer::debugMeshThread);
  QObject::connect(m_RemoveFilter, &QAction::triggered,
                   [this]() { SetMeshFilter(MeshFilter::None); });
  QObject::connect(m_FilterMesh, &QAction::triggered, [this]() {
    QModelIndex idx = m_CurView->selectionModel()->currentIndex();

    if(!idx.isValid())
      return;

    uint32_t taskIndex = 0, meshletIndex = 0;
    GetIndicesForMeshRow((uint32_t)idx.row(), taskIndex, meshletIndex);

    SetMeshFilter(MeshFilter::Mesh, taskIndex, meshletIndex);
  });
  QObject::connect(m_GotoTask, &QAction::triggered, [this]() {
    // if there's a filter then by definition only one task is visible, just scroll to it
    if(m_CurMeshFilter != MeshFilter::None)
    {
      ShowMeshData(MeshDataStage::TaskOut);
      ScrollToRow(0, MeshDataStage::TaskOut);
      return;
    }

    QModelIndex idx = m_CurView->selectionModel()->currentIndex();

    if(!idx.isValid())
      return;

    uint32_t taskIndex = 0, meshletIndex = 0;
    GetIndicesForMeshRow((uint32_t)idx.row(), taskIndex, meshletIndex);

    ShowMeshData(MeshDataStage::TaskOut);
    ScrollToRow((int)taskIndex, MeshDataStage::TaskOut);
  });

  QObject::connect(ui->exportDrop, &QToolButton::clicked,
                   [this] { exportData(BufferExport(BufferExport::CSV)); });

  ui->inTable->setContextMenuPolicy(Qt::CustomContextMenu);
  ui->out1Table->setContextMenuPolicy(Qt::CustomContextMenu);
  ui->out2Table->setContextMenuPolicy(Qt::CustomContextMenu);

  ui->fixedVars->setContextMenuPolicy(Qt::CustomContextMenu);

  ui->fixedVars->setFrameShape(QFrame::NoFrame);

  QMenu *menu = new QMenu(this);

  ui->inTable->setCustomHeaderSizing(true);
  ui->out1Table->setCustomHeaderSizing(true);
  ui->out2Table->setCustomHeaderSizing(true);

  ui->inTable->setAllowKeyboardSearches(false);
  ui->out1Table->setAllowKeyboardSearches(false);
  ui->out2Table->setAllowKeyboardSearches(false);

  QObject::connect(ui->fixedVars, &RDTreeWidget::customContextMenuRequested, this,
                   &BufferViewer::fixedVars_contextMenu);

  QObject::connect(ui->inTable, &RDTableView::customContextMenuRequested,
                   [this, menu](const QPoint &pos) { stageRowMenu(MeshDataStage::VSIn, menu, pos); });

  menu = new QMenu(this);

  QObject::connect(
      ui->out1Table, &RDTableView::customContextMenuRequested,
      [this, menu](const QPoint &pos) { stageRowMenu(MeshDataStage::VSOut, menu, pos); });

  menu = new QMenu(this);

  QObject::connect(
      ui->out2Table, &RDTableView::customContextMenuRequested,
      [this, menu](const QPoint &pos) { stageRowMenu(MeshDataStage::GSOut, menu, pos); });

  ui->dockarea->setAllowFloatingWindow(false);

  ui->controlType->addItems({tr("Arcball"), tr("Flycam")});
  ui->controlType->adjustSize();

  configureDrawRange();

  ui->visualisation->clear();
  ui->visualisation->addItems(
      {tr("None"), tr("Solid Colour"), tr("Flat Shaded"), tr("Secondary"), tr("Exploded")});
  ui->visualisation->adjustSize();
  ui->visualisation->setCurrentIndex(0);
  if(meshview)
    SetupMeshUV();

  ui->axisMappingCombo->addItems({tr("Y-up, left handed"), tr("Y-up, right handed"),
                                  tr("Z-up, left handed"), tr("Z-up, right handed"), tr("Custom...")});
  ui->axisMappingCombo->setCurrentIndex(0);

  // wireframe only available on solid shaded options
  ui->wireframeRender->setEnabled(m_MeshView);

  ui->setFormat->setVisible(false);

  ui->controlType->setCurrentIndex(0);
  on_controlType_currentIndexChanged(0);

  QObject::connect(ui->inTable->selectionModel(), &QItemSelectionModel::selectionChanged, this,
                   &BufferViewer::data_selected);
  QObject::connect(ui->out1Table->selectionModel(), &QItemSelectionModel::selectionChanged, this,
                   &BufferViewer::data_selected);
  QObject::connect(ui->out2Table->selectionModel(), &QItemSelectionModel::selectionChanged, this,
                   &BufferViewer::data_selected);

  m_CurView = ui->inTable;
  m_CurFixed = false;

  QObject::connect(ui->inTable, &RDTableView::clicked, [this]() {
    m_CurView = ui->inTable;
    m_CurFixed = false;
  });
  QObject::connect(ui->out1Table, &RDTableView::clicked, [this]() { m_CurView = ui->out1Table; });
  QObject::connect(ui->out2Table, &RDTableView::clicked, [this]() { m_CurView = ui->out2Table; });

  QObject::connect(ui->fixedVars, &RDTreeWidget::clicked, [this]() {
    m_CurView = NULL;
    m_CurFixed = true;
  });

  QObject::connect(ui->inTable->verticalScrollBar(), &QScrollBar::valueChanged, this,
                   &BufferViewer::data_scrolled);
  QObject::connect(ui->out1Table->verticalScrollBar(), &QScrollBar::valueChanged, this,
                   &BufferViewer::data_scrolled);
  QObject::connect(ui->out2Table->verticalScrollBar(), &QScrollBar::valueChanged, this,
                   &BufferViewer::data_scrolled);

  {
    QMenu *extensionsMenu = new QMenu(this);

    ui->extensions->setMenu(extensionsMenu);
    ui->extensions->setPopupMode(QToolButton::InstantPopup);

    QObject::connect(extensionsMenu, &QMenu::aboutToShow, [this, extensionsMenu]() {
      extensionsMenu->clear();
      m_Ctx.Extensions().MenuDisplaying(m_MeshView ? PanelMenu::MeshPreview : PanelMenu::BufferViewer,
                                        extensionsMenu, ui->extensions, {});
    });
  }

  QObject::connect(ui->render, &CustomPaintWidget::mouseMove, this, &BufferViewer::render_mouseMove);
  QObject::connect(ui->render, &CustomPaintWidget::clicked, this, &BufferViewer::render_clicked);
  QObject::connect(ui->render, &CustomPaintWidget::unclicked, this, &BufferViewer::render_unclicked);
  QObject::connect(ui->render, &CustomPaintWidget::keyPress, this, &BufferViewer::render_keyPress);
  QObject::connect(ui->render, &CustomPaintWidget::keyRelease, this,
                   &BufferViewer::render_keyRelease);
  QObject::connect(ui->render, &CustomPaintWidget::mouseWheel, this,
                   &BufferViewer::render_mouseWheel);

  // event filter to pick up tooltip events
  ui->fixedVars->setTooltipElidedItems(false);
  ui->fixedVars->installEventFilter(this);

  QObject::connect(m_MeshDebugSelector, &ComputeDebugSelector::beginDebug, this,
                   &BufferViewer::meshDebugSelector_beginDebug);

  Reset();

  m_Ctx.AddCaptureViewer(this);
}

void BufferViewer::GetIndicesForMeshRow(uint32_t row, uint32_t &taskIndex, uint32_t &meshletIdx)
{
  const BufferConfiguration &config2 = m_ModelOut2->getConfig();

  auto it = std::upper_bound(config2.meshletVertexPrefixCounts.begin(),
                             config2.meshletVertexPrefixCounts.end(), row);

  if(it != config2.meshletVertexPrefixCounts.begin())
    it--;

  meshletIdx = uint32_t(it - config2.meshletVertexPrefixCounts.begin());

  const BufferConfiguration &config1 = m_ModelOut1->getConfig();

  taskIndex = 0;

  uint32_t meshletCounter = 0;
  for(taskIndex = 0; taskIndex < meshletIdx && taskIndex < config1.taskSizes.size(); taskIndex++)
  {
    meshletCounter += config1.taskSizes[taskIndex].x * config1.taskSizes[taskIndex].y *
                      config1.taskSizes[taskIndex].z;

    if(meshletIdx < meshletCounter)
      break;
  }

  taskIndex += config1.taskOrMeshletOffset;
  meshletIdx += config2.taskOrMeshletOffset;
}

void BufferViewer::SetupRawView()
{
  ui->formatSpecifier->setVisible(true);
  ui->outputTabs->setVisible(false);
  ui->out1Table->setVisible(false);
  ui->out2Table->setVisible(false);

  m_Containers[0]->setVisible(false);
  m_Containers[1]->setVisible(false);
  m_Containers[2]->setVisible(false);

  // hide buttons we don't want in the toolbar
  ui->syncViews->setVisible(false);
  ui->instanceLabel->setVisible(false);
  ui->instance->setVisible(false);
  ui->viewLabel->setVisible(false);
  ui->viewIndex->setVisible(false);
  ui->dockarea->setVisible(false);

  ui->meshFilterLabel->setVisible(false);
  ui->resetMeshFilterButton->setVisible(false);

  ui->inTable->setFrameShape(QFrame::NoFrame);

  ui->inTable->setPinnedColumns(1);
  ui->inTable->setColumnGroupRole(columnGroupRole);

  m_delegate = new RichTextViewDelegate(ui->inTable);
  ui->inTable->setItemDelegate(m_delegate);

  ui->inTable->viewport()->installEventFilter(this);

  ui->inTable->setMouseTracking(true);

  ui->formatSpecifier->setWindowTitle(tr("Buffer Format"));

  QObject::connect(ui->formatSpecifier, &BufferFormatSpecifier::processFormat,
                   [this](const QString &format) {
                     m_PagingByteOffset = 0;
                     processFormat(format);
                   });

  ui->fixedVars->setColumns({tr("Name"), tr("Value"), tr("Byte Offset"), tr("Type")});
  {
    ui->fixedVars->header()->setSectionResizeMode(0, QHeaderView::Interactive);
    ui->fixedVars->header()->setSectionResizeMode(1, QHeaderView::Interactive);
    ui->fixedVars->header()->setSectionResizeMode(2, QHeaderView::Interactive);
  }

  ui->fixedVars->setFont(Formatter::FixedFont());

  m_FixedGroup = new CollapseGroupBox(this);
  m_RepeatedGroup = new CollapseGroupBox(this);

  m_RepeatedControlBar = new QFrame(this);

  m_RepeatedControlBar->setFrameShape(QFrame::Panel);
  m_RepeatedControlBar->setFrameShadow(QFrame::Raised);

  QHBoxLayout *controlLayout = new QHBoxLayout(m_RepeatedControlBar);
  controlLayout->setSpacing(2);
  controlLayout->setContentsMargins(6, 2, 6, 2);

  m_RepeatedOffset = new RDLabel(this);

  QFrame *line = new QFrame(this);
  line->setFrameShape(QFrame::VLine);
  line->setFrameShadow(QFrame::Sunken);

  controlLayout->addWidget(line);

  controlLayout->addWidget(m_RepeatedOffset);

  controlLayout->addItem(new QSpacerItem(40, 20, QSizePolicy::Expanding, QSizePolicy::Minimum));

  QVBoxLayout *fixedLayout = new QVBoxLayout(m_FixedGroup);
  fixedLayout->setSpacing(0);
  fixedLayout->setContentsMargins(0, 0, 0, 0);

  QVBoxLayout *repeatedLayout = new QVBoxLayout(m_RepeatedGroup);
  repeatedLayout->setSpacing(3);
  repeatedLayout->setContentsMargins(2, 0, 0, 0);

  repeatedLayout->addWidget(m_RepeatedControlBar);

  m_FixedGroup->setTitle(tr("Fixed SoA data"));
  m_RepeatedGroup->setTitle(tr("Repeated AoS values"));

  m_VLayout = new QVBoxLayout(this);
  m_VLayout->setSpacing(3);
  m_VLayout->setContentsMargins(3, 3, 3, 3);

  m_OuterSplitter = new RDSplitter(Qt::Vertical, this);
  m_OuterSplitter->setHandleWidth(12);
  m_OuterSplitter->setChildrenCollapsible(false);

  m_InnerSplitter = new RDSplitter(Qt::Vertical, this);
  m_InnerSplitter->setHandleWidth(12);
  m_InnerSplitter->setChildrenCollapsible(false);

  m_InnerSplitter->setVisible(false);

  // inner splitter is only used when we have these groups, so we can add these unconditionally
  m_InnerSplitter->addWidget(m_FixedGroup);
  m_InnerSplitter->addWidget(m_RepeatedGroup);

  m_VLayout->addWidget(ui->meshToolbar);
  UI_SetupCBufferSearch();
  // 0 will be variable, but set it to something here so QSplitter doesn't barf
  m_OuterSplitter->insertWidget(0, ui->inTable);
  m_OuterSplitter->insertWidget(1, ui->formatSpecifier);
  m_VLayout->addWidget(m_OuterSplitter);
  UI_SetupCBufferLiveEdit();
}

void BufferViewer::UI_SetupCBufferSearch()
{
  m_CBufferSearch = new QLineEdit(this);
  m_CBufferSearch->setObjectName(QStringLiteral("cbufferSearch"));
  m_CBufferSearch->setClearButtonEnabled(true);
  m_CBufferSearch->setPlaceholderText(tr("Search CBuffer parameters (Ctrl+F5)"));
  m_CBufferSearch->setAccessibleName(tr("Search CBuffer parameters"));
  m_CBufferSearch->setToolTip(tr(
      "Search names, original names, values, types or byte offsets. Separate terms with spaces."));
  m_CBufferSearch->installEventFilter(this);
  m_VLayout->addWidget(m_CBufferSearch);
  m_CBufferSearch->hide();
  m_CBufferSearchEmpty = new QLabel(tr("No matching CBuffer parameters."), this);
  m_CBufferSearchEmpty->setObjectName(QStringLiteral("cbufferSearchEmpty"));
  m_VLayout->addWidget(m_CBufferSearchEmpty);
  m_CBufferSearchEmpty->hide();
  QObject::connect(m_CBufferSearch, &QLineEdit::textChanged, this,
                   [this](const QString &) { UI_UpdateCBufferSearch(); });
  QObject::connect(m_CBufferSearch, &QLineEdit::returnPressed, this, [this]() {
    QModelIndex current = ui->fixedVars->model()->index(0, 0);
    while(current.isValid() && ui->fixedVars->isRowHidden(current.row(), current.parent()))
      current = current.sibling(current.row() + 1, 0);
    if(current.isValid())
    {
      ui->fixedVars->setCurrentIndex(current);
      ui->fixedVars->scrollTo(current);
      ui->fixedVars->setFocus();
    }
  });
  // Each shortcut belongs to its viewer, so multiple CBuffer tabs cannot steal one another's focus.
  for(const QKeySequence &key :
      {QKeySequence(Qt::CTRL | Qt::Key_F5), QKeySequence(QKeySequence::Find)})
  {
    QShortcut *find = new QShortcut(key, this);
    find->setContext(Qt::WidgetWithChildrenShortcut);
    QObject::connect(find, &QShortcut::activated, this, [this]() {
      if(IsCBufferView())
      {
        m_CBufferSearch->show();
        m_CBufferSearch->setFocus();
        m_CBufferSearch->selectAll();
      }
    });
  }
}

void BufferViewer::UI_UpdateCBufferSearch()
{
  if(!m_CBufferSearch || !IsCBufferView())
    return;
  const QStringList terms =
      m_CBufferSearch->text().simplified().split(QLatin1Char(' '), QString::SkipEmptyParts);
  const bool searching = !terms.isEmpty();
  if(searching && !m_CBufferSearchActive)
    ui->fixedVars->saveExpansion(m_CBufferSearchExpansion, 0);
  QSet<const RDTreeWidgetItem *> visible;
  RDTreeWidgetItem *root = ui->fixedVars->invisibleRootItem();
  for(int row = 0; row < root->childCount(); row++)
    CBufferSearchVisibleItems(root->child(row), terms, visible);
  // Filtering changes only view visibility. Physical row identities, semantic lane mappings,
  // selection and pending live writes remain untouched; no replay or buffer decoding is needed.
  const bool updatesEnabled = ui->fixedVars->updatesEnabled();
  ui->fixedVars->setUpdatesEnabled(false);
  std::function<void(const QModelIndex &)> apply = [&](const QModelIndex &parent) {
    for(int row = 0; row < ui->fixedVars->model()->rowCount(parent); row++)
    {
      const QModelIndex index = ui->fixedVars->model()->index(row, 0, parent);
      const RDTreeWidgetItem *item = ui->fixedVars->itemForIndex(index);
      ui->fixedVars->setRowHidden(row, parent, !visible.contains(item));
      if(searching && visible.contains(item) && item->childCount() > 0)
        ui->fixedVars->setExpanded(index, true);
      apply(index);
    }
  };
  apply(QModelIndex());
  if(!searching && m_CBufferSearchActive)
    ui->fixedVars->applyExpansion(m_CBufferSearchExpansion, 0);
  m_CBufferSearchActive = searching;
  m_CBufferSearchEmpty->setVisible(searching && visible.isEmpty());
  ui->fixedVars->setUpdatesEnabled(updatesEnabled);
}

void BufferViewer::UI_SetupCBufferLiveEdit()
{
  m_CBufferPin = new QToolButton(this);
  m_CBufferPin->setObjectName(QStringLiteral("cbufferPin"));
  m_CBufferPin->setText(tr("Pin"));
  m_CBufferPin->setToolTip(tr("Keep this CBuffer and its EID when selecting other events"));
  m_CBufferPin->setCheckable(true);
  m_CBufferPin->setAutoRaise(true);
  m_CBufferPin->setEnabled(false);
  m_CBufferPin->hide();
  setProperty("cbufferPinned", false);
  setProperty("cbufferPinnedEvent", 0);
  setProperty("cbufferDisplayedEvent", 0);
  ui->horizontalLayout_9->insertWidget(2, m_CBufferPin);
  QObject::connect(m_CBufferPin, &QToolButton::toggled, this, &BufferViewer::UI_SetCBufferPinned);
  m_CBufferLiveEdit.reset(new CBufferLiveEdit);
  CBufferLiveEdit &edit = *m_CBufferLiveEdit;
  edit.panel = new QWidget(this);
  edit.panel->setObjectName(QStringLiteral("cbufferLiveEditor"));
  edit.panel->setProperty("busy", false);
  edit.panel->setProperty("pending", false);
  edit.panel->setProperty("error", QString());
  QVBoxLayout *layout = new QVBoxLayout(edit.panel);
  layout->setContentsMargins(3, 3, 3, 3);
  QHBoxLayout *heading = new QHBoxLayout;
  edit.name = new QLabel(tr("Select a CBuffer value to edit it live"), edit.panel);
  heading->addWidget(edit.name, 1);
  edit.color = new QPushButton(tr("Color..."), edit.panel);
  edit.color->setObjectName(QStringLiteral("cbufferLiveColor"));
  edit.color->hide();
  heading->addWidget(edit.color);
  edit.reset = new QPushButton(tr("Restore captured value"), edit.panel);
  edit.reset->setToolTip(tr("Remove edits for this parameter and restore its original captured value"));
  edit.reset->setObjectName(QStringLiteral("cbufferLiveReset"));
  edit.reset->setEnabled(false);
  heading->addWidget(edit.reset);
  layout->addLayout(heading);
  QHBoxLayout *channels = new QHBoxLayout;
  for(int component = 0; component < 4; component++)
  {
    edit.channels[component] = new QWidget(edit.panel);
    QVBoxLayout *channel = new QVBoxLayout(edit.channels[component]);
    channel->setContentsMargins(0, 0, 0, 0);
    QDoubleSpinBox *value = new CBufferValueSpinBox(edit.channels[component]);
    value->setObjectName(QStringLiteral("cbufferLiveValue%1").arg(component));
    value->setKeyboardTracking(false);
    value->setMinimumWidth(100);
    edit.values[component] = value;
    edit.channelLabels[component] = new QLabel(edit.channels[component]);
    edit.channelLabels[component]->setObjectName(
        QStringLiteral("cbufferLiveChannel%1").arg(component));
    QHBoxLayout *valueRow = new QHBoxLayout;
    valueRow->addWidget(edit.channelLabels[component]);
    valueRow->addWidget(value, 1);
    QSlider *slider = new QSlider(Qt::Horizontal, edit.channels[component]);
    slider->setObjectName(QStringLiteral("cbufferLiveSlider%1").arg(component));
    slider->setRange(-1000, 1000);
    slider->setToolTip(
        tr("Drag to adjust; type an exact value above. Release to recenter the slider."));
    edit.sliders[component] = slider;
    channel->addLayout(valueRow);
    channel->addWidget(slider);
    channels->addWidget(edit.channels[component]);
    edit.channels[component]->hide();
    QObject::connect(value, QOverload<double>::of(&QDoubleSpinBox::valueChanged), this,
                     [this, component](double) { UI_QueueCBufferLiveValue(component); });
    QObject::connect(slider, &QSlider::sliderPressed, this, [this, component]() {
      m_CBufferLiveEdit->dragBase[component] = m_CBufferLiveEdit->values[component]->value();
    });
    QObject::connect(slider, &QSlider::valueChanged, this, [this, component](int position) {
      CBufferLiveEdit &live = *m_CBufferLiveEdit;
      const double base = live.dragBase[component];
      double value = base + position * qMax(1.0, fabs(base)) / 500.0;
      if(live.type != VarType::Float)
        value = round(value);
      live.values[component]->setValue(value);
    });
    QObject::connect(slider, &QSlider::sliderReleased, this, [this, component]() {
      CBufferLiveEdit &live = *m_CBufferLiveEdit;
      const QSignalBlocker blocker(live.sliders[component]);
      live.sliders[component]->setValue(0);
      live.dragBase[component] = live.values[component]->value();
      UI_SubmitCBufferLiveValues();
    });
  }
  layout->addLayout(channels);
  edit.status = new QLabel(
      tr("Edits affect all uses of this shared buffer range. No shader compilation is needed."),
      edit.panel);
  edit.status->setObjectName(QStringLiteral("cbufferLiveStatus"));
  edit.status->setWordWrap(true);
  layout->addWidget(edit.status);
  m_VLayout->addWidget(edit.panel);
  edit.panel->hide();
  edit.timer = new QTimer(this);
  edit.timer->setObjectName(QStringLiteral("cbufferLiveSubmitTimer"));
  edit.timer->setSingleShot(true);
  // Merge changes in the current GUI turn without delaying the first preview by a frame.
  // An in-flight replay already coalesces later inputs through the pending-value map.
  edit.timer->setInterval(
      qEnvironmentVariableIntValue("RENDERDOC_CBUFFER_EDIT_LEGACY_REFRESH") == 1
          ? 80
          : qBound(0, qEnvironmentVariableIntValue("RENDERDOC_CBUFFER_EDIT_DEBOUNCE_MS"), 80));
  QObject::connect(edit.timer, &QTimer::timeout, this, &BufferViewer::UI_SubmitCBufferLiveValues);
  QObject::connect(ui->fixedVars, &RDTreeWidget::currentItemChanged, this,
                   [this](RDTreeWidgetItem *item, RDTreeWidgetItem *) {
                     if(item)
                       UI_SelectCBufferLiveValue(item);
                   });
  QObject::connect(ui->fixedVars, &RDTreeWidget::itemDoubleClicked, this,
                   [this](RDTreeWidgetItem *item, int column) {
                     UI_SelectCBufferLiveValue(item);
                     if(column == 1 && !m_CBufferLiveEdit->offsets.isEmpty())
                     {
                       m_CBufferLiveEdit->values[0]->setFocus();
                       m_CBufferLiveEdit->values[0]->selectAll();
                     }
                   });
  // A replay/alias refresh can invalidate the editor while retaining the same selected row.
  // Clicking that row again must reopen its controls even without currentItemChanged.
  QObject::connect(ui->fixedVars, &RDTreeWidget::itemClicked, this,
                   [this](RDTreeWidgetItem *item, int) { UI_SelectCBufferLiveValue(item); });
  QObject::connect(edit.reset, &QPushButton::clicked, this, [this]() {
    CBufferLiveEdit &live = *m_CBufferLiveEdit;
    if(live.key.isEmpty() || live.offsets.isEmpty())
      return;
    // The selected lanes describe a removal, not a value write. This also works after reopening
    // the panel or after another editor applied the value; no editor-entry snapshot is needed.
    QMap<quint64, uint32_t> lanes;
    for(quint64 offset : live.offsets)
      lanes[offset] = 0;
    live.pending[live.key] = lanes;
    live.resets.insert(live.key);
    if(!live.oldestInput.isValid())
      live.oldestInput.start();
    live.newestInput.start();
    ++live.queuedSamples;
    live.panel->setProperty("pending", true);
    live.panel->setProperty("error", QString());
    UI_SubmitCBufferLiveValues();
  });
  QObject::connect(edit.color, &QPushButton::clicked, this, [this]() {
    CBufferLiveEdit &live = *m_CBufferLiveEdit;
    if(live.offsets.size() < 3 || live.colorDialog)
      return;
    const QString key = live.key;
    QVector<double> before;
    for(int i = 0; i < live.offsets.size(); i++)
      before.push_back(live.values[i]->value());
    const double intensity = qMax(1.0, qMax(before[0], qMax(before[1], before[2])));
    QColor initial = QColor::fromRgbF(qBound(0.0, before[0] / intensity, 1.0),
                                      qBound(0.0, before[1] / intensity, 1.0),
                                      qBound(0.0, before[2] / intensity, 1.0));
    CBufferColorDialog *dialog = new CBufferColorDialog(initial, this);
    dialog->setWindowTitle(tr("CBuffer color (HDR intensity is preserved)"));
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    live.colorDialog = dialog;
    dialog->colorChanged = [this, key, intensity](const QColor &color) {
      CBufferLiveEdit &live = *m_CBufferLiveEdit;
      if(live.key != key)
        return;
      live.settingValues = true;
      live.values[0]->setValue(color.redF() * intensity);
      live.values[1]->setValue(color.greenF() * intensity);
      live.values[2]->setValue(color.blueF() * intensity);
      live.settingValues = false;
      UI_QueueCBufferLiveValue(0);
    };
    QObject::connect(dialog, &QDialog::accepted, this, &BufferViewer::UI_SubmitCBufferLiveValues);
    QObject::connect(dialog, &QDialog::rejected, this, [this, key, before]() {
      CBufferLiveEdit &live = *m_CBufferLiveEdit;
      if(live.key != key)
        return;
      live.settingValues = true;
      for(int i = 0; i < before.size(); i++)
        live.values[i]->setValue(before[i]);
      live.settingValues = false;
      UI_QueueCBufferLiveValue(0);
      UI_SubmitCBufferLiveValues();
    });
    dialog->open();
  });
}

const ShaderReflection *BufferViewer::CBufferReflection() const
{
  return m_CBufferPinnedReflection ? m_CBufferPinnedReflection.get()
                                  : m_Ctx.CurPipelineState().GetShaderReflection(m_CBufferSlot.stage);
}

uint32_t BufferViewer::CBufferEvent() const
{
  return m_CBufferPinnedReflection ? m_CBufferPinnedEvent : m_Ctx.CurEvent();
}

void BufferViewer::UI_SetCBufferPinned(bool pinned)
{
  UI_InvalidateCBufferLiveEdit();
  ++m_Sequence;
  m_HasDeferredEvent = false;
  if(pinned && IsCBufferView() && m_Ctx.IsCaptureLoaded() && m_CurCBuffer.valid &&
     m_CBufferDisplayedEvent == m_Ctx.CurEvent())
  {
    const ShaderReflection *reflection = CBufferReflection();
    if(reflection)
    {
      m_CBufferPinnedEvent = m_CBufferDisplayedEvent;
      m_CBufferPinnedBinding = m_Ctx.CurPipelineState().GetConstantBlock(
          m_CBufferSlot.stage, m_CBufferSlot.slot, m_CBufferSlot.arrayIdx);
      m_CBufferPinnedData = m_CurCBuffer;
      m_CBufferPinnedReflection.reset(new ShaderReflection(*reflection));
      m_CBufferPin->setToolTip(tr("Pinned to EID %1. Click to follow event selection again.")
                                  .arg(m_CBufferPinnedEvent));
    }
  }
  else
  {
    m_CBufferPinnedReflection.reset();
    m_CBufferPinnedEvent = 0;
  }
  const bool active = bool(m_CBufferPinnedReflection);
  const QSignalBlocker blocker(m_CBufferPin);
  m_CBufferPin->setChecked(active);
  setProperty("cbufferPinned", active);
  setProperty("cbufferPinnedEvent", m_CBufferPinnedEvent);
  if(!active)
  {
    m_CBufferPin->setEnabled(false);
    m_CBufferPin->setToolTip(tr("Keep this CBuffer and its EID when selecting other events"));
    if(m_Ctx.IsCaptureLoaded())
      OnEventChanged(CBufferEvent());
  }
  else
  {
    updateLabelsAndLayout();
    UI_UpdateCBufferAliasWatch();
    if(ui->fixedVars->currentItem())
      UI_SelectCBufferLiveValue(ui->fixedVars->currentItem());
  }
}

void BufferViewer::UI_InvalidateCBufferLiveEdit()
{
  if(!m_CBufferLiveEdit)
    return;
  CBufferLiveEdit &live = *m_CBufferLiveEdit;
  ++*live.epoch;
  live.timer->stop();
  live.pending.clear();
  live.resets.clear();
  live.currentBits.clear();
  live.oldestInput.invalidate();
  live.newestInput.invalidate();
  live.queuedSamples = 0;
  live.offsets.clear();
  live.key.clear();
  live.scope.clear();
  live.generationKnown = false;
  live.panel->setProperty("pending", false);
  live.panel->setProperty("error", QString());
  live.reset->setEnabled(false);
  live.color->hide();
  for(QWidget *channel : live.channels)
    channel->hide();
  if(live.colorDialog)
    live.colorDialog->close();
  live.name->setText(tr("Select a CBuffer value to edit it live"));
  live.status->setText(tr("Edits affect all uses of this shared buffer range. No shader compilation is needed."));
}

void BufferViewer::UI_SelectCBufferLiveValue(RDTreeWidgetItem *item)
{
  if(!m_CBufferLiveEdit || !IsCBufferView() || !item || !m_Ctx.IsCaptureLoaded())
    return;
  CBufferLiveEdit &live = *m_CBufferLiveEdit;
  const FixedVarTag tag = item->tag().value<FixedVarTag>();
  const ShaderReflection *reflection =
      CBufferReflection();
  const ConstantBlock *block = reflection && m_CBufferSlot.slot < reflection->constantBlocks.size()
                                   ? &reflection->constantBlocks[m_CBufferSlot.slot]
                                   : NULL;
  const bool available = block && m_CurCBuffer.valid && m_CurCBuffer.bytesBacked &&
                         !m_CurCBuffer.compileConstants && !block->inlineDataBytes &&
                         m_Format.isEmpty() && m_BufferID != ResourceId() &&
                         !tag.replayLabEditOffsets.isEmpty() &&
                         !(tag.replayLabDisplayValue.flags & ShaderVariableFlags::Truncated);
  live.panel->show();
  if(!available)
  {
    live.offsets.clear();
    live.key.clear();
    for(QWidget *channel : live.channels)
      channel->hide();
    live.color->hide();
    live.reset->setEnabled(false);
    live.name->setText(
        tr("Only reflected 32-bit float/int/uint scalar and vector leaves are editable"));
    return;
  }
  const QString shaderHash = ReplayLabCurrentShaderRawSha256(reflection);
  const QString scope = QStringLiteral("%1:%2:%3:%4:%5:%6")
                            .arg(ToQStr(m_BufferID))
                            .arg(m_ByteOffset)
                            .arg(CBufferEvent())
                            .arg(int(m_CBufferSlot.stage))
                            .arg(m_CBufferSlot.slot)
                            .arg(shaderHash);
  if(live.scope != scope)
    UI_InvalidateCBufferLiveEdit();
  live.scope = scope;
  live.shaderHash = shaderHash;
  live.eventId = CBufferEvent();
  live.buffer = m_BufferID;
  live.bindingOffset = m_ByteOffset;
  live.bindingSize = qMin(uint64_t(block->byteSize), m_ByteSize);
  live.stage = m_CBufferSlot.stage;
  live.blockIndex = m_CBufferSlot.slot;
  live.arrayIndex = m_CBufferSlot.arrayIdx;
  QStringList laneKey;
  for(quint64 offset : tag.replayLabEditOffsets)
  {
    if(offset > live.bindingSize || live.bindingSize - offset < 4)
      return;
    laneKey.push_back(QString::number(offset));
  }
  const QString key = scope + QLatin1Char(':') + laneKey.join(QLatin1Char(','));
  if(live.key == key && (live.inFlight || !live.pending.isEmpty() ||
                        (live.panel->property("error").toString().isEmpty() &&
                         CBufferLiveInputActive(live))))
    return;    // Do not replace a focused numeric editor during an asynchronous publication.
  live.key = key;
  live.offsets = tag.replayLabEditOffsets;
  live.type = tag.replayLabBaseType;
  live.name->setText(tr("Live CBuffer: %1").arg(item->text(0)));
  const bool colorField = CBufferColorField(item->text(0), live.type, live.offsets.size());
  live.settingValues = true;
  for(int component = 0; component < 4; component++)
  {
    live.channels[component]->setVisible(component < live.offsets.size());
    if(component >= live.offsets.size())
      continue;
    live.channelLabels[component]->setVisible(live.offsets.size() > 1);
    // i18n-ignore: graphics vector/color component identifiers, never translated prose.
    live.channelLabels[component]->setText(
        QString(QLatin1Char(colorField ? "RGBA"[component] : "XYZW"[component])));
    QDoubleSpinBox *spin = live.values[component];
    spin->setDecimals(live.type == VarType::Float ? 323 : 0);
    spin->setRange(live.type == VarType::UInt   ? 0.0
                   : live.type == VarType::SInt ? double(INT32_MIN)
                                                : -double(FLT_MAX),
                   live.type == VarType::UInt   ? double(UINT32_MAX)
                   : live.type == VarType::SInt ? double(INT32_MAX)
                                                : double(FLT_MAX));
    const ShaderValue &value = tag.replayLabDisplayValue.value;
    const double number = live.type == VarType::Float  ? double(value.f32v[component])
                          : live.type == VarType::SInt ? double(value.s32v[component])
                                                       : double(value.u32v[component]);
    spin->setEnabled(std::isfinite(number));
    live.sliders[component]->setEnabled(std::isfinite(number));
    spin->setValue(number);
    spin->setSingleStep(live.type == VarType::Float ? qMax(0.001, fabs(number) * 0.01) : 1.0);
    live.dragBase[component] = number;
    const QSignalBlocker blocker(live.sliders[component]);
    live.sliders[component]->setValue(0);
  }
  live.settingValues = false;
  live.color->setVisible(colorField);
  live.reset->setEnabled(!live.offsets.isEmpty());
}

void BufferViewer::UI_RestoreCBufferLiveSelection()
{
  if(!m_CBufferLiveEdit || m_CBufferLiveEdit->offsets.isEmpty())
    return;
  const QList<quint64> offsets = m_CBufferLiveEdit->offsets;
  RDTreeWidgetItem *found = NULL;
  std::function<void(RDTreeWidgetItem *)> find = [&](RDTreeWidgetItem *parent) {
    for(int i = 0; i < parent->childCount() && !found; i++)
    {
      RDTreeWidgetItem *item = parent->child(i);
      if(item->tag().value<FixedVarTag>().replayLabEditOffsets == offsets)
        found = item;
      else
        find(item);
    }
  };
  find(ui->fixedVars->invisibleRootItem());
  if(found)
  {
    const QSignalBlocker blocker(ui->fixedVars);
    ui->fixedVars->setCurrentItem(found);
    UI_SelectCBufferLiveValue(found);
  }
  else
    UI_InvalidateCBufferLiveEdit();
}

void BufferViewer::NavigateCBufferVariable(const QString &path, qint64 byteOffset, int component)
{
  m_CBufferNavigationPath = path;
  m_CBufferNavigationOffset = byteOffset;
  m_CBufferNavigationComponent = component;
  m_CBufferNavigationEvent = m_Ctx.CurEvent();
  setProperty("cbufferNavigationStatus", QStringLiteral("pending"));
  if(m_CBufferSearch)
    m_CBufferSearch->clear();
  UI_ApplyCBufferNavigation();
}

void BufferViewer::UI_ApplyCBufferNavigation()
{
  if(m_CBufferNavigationOffset < 0 || !IsCBufferView() ||
     m_CBufferNavigationEvent != m_Ctx.CurEvent() ||
     m_CBufferDisplayedEvent != m_CBufferNavigationEvent)
    return;
  RDTreeWidgetItem *found = NULL;
  int best = -1;
  const quint64 lane =
      quint64(m_CBufferNavigationOffset) + quint64(qMax(0, m_CBufferNavigationComponent)) * 4;
  std::function<void(RDTreeWidgetItem *)> visit = [&](RDTreeWidgetItem *root) {
    for(int i = 0; i < root->childCount(); ++i)
    {
      RDTreeWidgetItem *item = root->child(i);
      const FixedVarTag tag = item->tag().value<FixedVarTag>();
      int score = -1;
      if(tag.replayLabEditOffsets.contains(lane))
        score = tag.replayLabEditOffsets.size() == 1 ? 3 : 1;
      if(tag.path == m_CBufferNavigationPath && tag.byteOffset == m_CBufferNavigationOffset)
        score = qMax(score, 2);
      if(score > best)
      {
        best = score;
        found = item;
      }
      visit(item);
    }
  };
  visit(ui->fixedVars->invisibleRootItem());
  if(!found)
    return;    // The asynchronous reflected rows may still be loading.
  for(RDTreeWidgetItem *parent = found->parent(); parent; parent = parent->parent())
    ui->fixedVars->expandItem(parent);
  ui->fixedVars->setCurrentItem(found);
  ui->fixedVars->setSelectedItem(found);
  ui->fixedVars->scrollToItem(found);
  UI_SelectCBufferLiveValue(found);
  setProperty("cbufferNavigationPath", m_CBufferNavigationPath);
  setProperty("cbufferNavigationOffset", m_CBufferNavigationOffset);
  setProperty("cbufferNavigationComponent", m_CBufferNavigationComponent);
  setProperty("cbufferNavigationStatus", QStringLiteral("selected"));
  if(m_CBufferLiveEdit && m_CBufferNavigationComponent >= 0)
  {
    const QList<quint64> offsets = found->tag().value<FixedVarTag>().replayLabEditOffsets;
    const int channel = offsets.indexOf(lane);
    if(channel >= 0 && channel < 4 && m_CBufferLiveEdit->values[channel]->isVisible())
    {
      m_CBufferLiveEdit->values[channel]->setFocus();
      m_CBufferLiveEdit->values[channel]->selectAll();
    }
  }
  m_CBufferNavigationOffset = -1;
}

void BufferViewer::UI_QueueCBufferLiveValue(int component)
{
  if(!m_CBufferLiveEdit || m_CBufferLiveEdit->settingValues)
    return;
  CBufferLiveEdit &live = *m_CBufferLiveEdit;
  if(component >= live.offsets.size() || live.key.isEmpty())
    return;
  // Queue the complete semantic field, then merge contiguous physical lanes below. RGB/RGBA
  // changes replay once; a scattered field never overwrites bytes belonging to another field.
  QMap<quint64, uint32_t> values;
  for(int i = 0; i < live.offsets.size(); i++)
  {
    uint32_t bits;
    if(!live.values[i]->isEnabled() || !CBufferLiveLaneBits(live.type, live.values[i]->value(), bits))
      return;
    values[live.offsets[i]] = bits;
  }
  live.pending[live.key] = values;
  // Cancel at input time, including while an older value is still replaying. Waiting for its
  // publication would let an obsolete idle prewarm compete with the newest drag value.
  m_Ctx.CancelReplayBackgroundWork();
  if(!live.oldestInput.isValid())
    live.oldestInput.start();
  live.newestInput.start();
  ++live.queuedSamples;
  live.resets.remove(live.key);
  live.panel->setProperty("pending", true);
  live.panel->setProperty("error", QString());
  if(!live.timer->isActive())
    live.timer->start();
}

void BufferViewer::UI_SubmitCBufferLiveValues()
{
  if(!m_CBufferLiveEdit || !m_Ctx.IsCaptureLoaded())
    return;
  CBufferLiveEdit &live = *m_CBufferLiveEdit;
  if(live.inFlight || live.pending.isEmpty())
    return;
  live.timer->stop();
  const QMap<QString, QMap<quint64, uint32_t>> pending = live.pending;
  const QSet<QString> resets = live.resets;
  const QMap<quint64, uint32_t> knownCurrent = live.currentBits;
  const QElapsedTimer oldestInput = live.oldestInput;
  const QElapsedTimer newestInput = live.newestInput;
  const uint32_t inputSamples = live.queuedSamples;
  live.oldestInput.invalidate();
  live.newestInput.invalidate();
  live.queuedSamples = 0;
  QElapsedTimer submitClock;
  submitClock.start();
  const double inputWaitMS = oldestInput.isValid() ? oldestInput.nsecsElapsed() / 1.0e6 : 0.0;
  live.pending.clear();
  live.resets.clear();
  live.inFlight = true;
  live.panel->setProperty("busy", true);
  live.panel->setProperty("pending", false);
  const auto epoch = live.epoch;
  const uint64_t request = epoch->load();
  const ResourceId buffer = live.buffer;
  const uint64_t bindingOffset = live.bindingOffset;
  const uint64_t bindingSize = live.bindingSize;
  const QString shaderHash = live.shaderHash;
  const uint32_t eventId = live.eventId, blockIndex = live.blockIndex, arrayIndex = live.arrayIndex;
  const ShaderStage stage = live.stage;
  const bool pinned = bool(m_CBufferPinnedReflection);
  const bool generationKnown = live.generationKnown;
  const uint64_t expectedGeneration = live.generation;
  QPointer<BufferViewer> guard(this);
  m_Ctx.Replay().AsyncInvoke([guard, epoch, request, buffer, bindingOffset, bindingSize, shaderHash,
                              eventId, blockIndex, arrayIndex, stage, pinned, generationKnown,
                              expectedGeneration, pending, resets,
                              knownCurrent, oldestInput, newestInput, inputSamples, submitClock,
                              inputWaitMS](IReplayController *r) {
    const double queueMS = submitClock.nsecsElapsed() / 1.0e6;
    QElapsedTimer operationClock;
    operationClock.start();
    double readbackMS = 0.0, mutationMS = 0.0;
    uint64_t readbackBytes = 0;
    uint32_t nativeChanges = 0;
    QString error;
    bool externalChange = false;
    QMap<quint64, uint32_t> accepted;
    uint64_t generation = r->GetSharedBufferPatchGeneration();
    const bool discardCachedValues = generationKnown && generation != expectedGeneration;
    CBufferReplayEventScope replayEvent(r, pinned && epoch->load() == request ? eventId : 0);
    const uint32_t publicationEvent = replayEvent.previous;
    if(epoch->load() != request || r->GetCurrentEventID() != eventId)
      error = QCoreApplication::translate("BufferViewer",
                                          "The CBuffer binding changed; select the value again.");
    const PipeState &pipe = r->GetPipelineState();
    const UsedDescriptor binding = pipe.GetConstantBlock(stage, blockIndex, arrayIndex);
    if(error.isEmpty() &&
       (binding.descriptor.resource != buffer || binding.descriptor.byteOffset != bindingOffset ||
        binding.descriptor.byteSize < bindingSize ||
        ReplayLabCurrentShaderRawSha256(pipe.GetShaderReflection(stage)) != shaderHash))
      error = QCoreApplication::translate("BufferViewer",
                                          "The CBuffer binding changed; select the value again.");
    bool resetOnly = !pending.isEmpty();
    for(auto field = pending.begin(); field != pending.end(); ++field)
      resetOnly &= resets.contains(field.key());
    if(error.isEmpty() && discardCachedValues && !resetOnly)
    {
      externalChange = true;
      error = QCoreApplication::translate("BufferViewer",
                                          "Another editor changed this buffer. Values were "
                                          "refreshed; select the parameter again before editing.");
    }
    for(auto field = pending.begin(); error.isEmpty() && field != pending.end(); ++field)
    {
      const QList<CBufferLiveRun> runs = CBufferLiveRuns(field.value());
      for(const CBufferLiveRun &run : runs)
      {
        if(!error.isEmpty() || epoch->load() != request)
          break;
        const quint64 start = run.offset;
        const bytebuf &bytes = run.bytes;
        const quint64 next = start + bytes.size();
        if(next > bindingSize || bindingOffset > UINT64_MAX - next)
        {
          error = QCoreApplication::translate(
              "BufferViewer", "The CBuffer binding changed; select the value again.");
          break;
        }
        const bool reset = resets.contains(field.key());
        bytebuf before;
        bool cached = !reset;
        for(quint64 lane = start; lane < next; lane += 4)
          cached &= knownCurrent.contains(lane);
        // A reset needs no pre-edit readback: the controller owns the capture baseline.
        if(!reset && cached)
        {
          before.resize(bytes.size());
          for(quint64 lane = start; lane < next; lane += 4)
          {
            const uint32_t bits = knownCurrent.value(lane);
            memcpy(before.data() + size_t(lane - start), &bits, 4);
          }
        }
        else if(!reset)
        {
          const qint64 begin = operationClock.nsecsElapsed();
          before = r->GetBufferData(buffer, bindingOffset + start, bytes.size());
          readbackMS += (operationClock.nsecsElapsed() - begin) / 1.0e6;
          readbackBytes += before.size();
        }
        if(!reset && before.size() != bytes.size())
        {
          error = QCoreApplication::translate(
              "BufferViewer", "The CBuffer bytes could not be read; no new value was applied.");
          break;
        }
        if(epoch->load() != request)
          break;
        const qint64 mutationBegin = operationClock.nsecsElapsed();
        const uint64_t beforeGeneration = r->GetSharedBufferPatchGeneration();
        // The capture is the source of truth. Clearing only these physical lanes also
        // restores edits made before this viewer opened, without touching other fields.
        if(reset)
          error = QString(r->ResetSharedBufferRange(buffer, bindingOffset + start, bytes.size()));
        else if(before != bytes)
          error = QString(r->SetSharedBufferPatch(buffer, bindingOffset + start, bytes));
        mutationMS += (operationClock.nsecsElapsed() - mutationBegin) / 1.0e6;
        nativeChanges += r->GetSharedBufferPatchGeneration() != beforeGeneration ? 1U : 0U;
        bytebuf restored;
        if(error.isEmpty() && reset)
        {
          const qint64 begin = operationClock.nsecsElapsed();
          restored = r->GetBufferData(buffer, bindingOffset + start, bytes.size());
          readbackMS += (operationClock.nsecsElapsed() - begin) / 1.0e6;
          readbackBytes += restored.size();
          if(restored.size() != bytes.size())
            error = QCoreApplication::translate(
                "BufferViewer", "The restored CBuffer value could not be read. Refresh the view.");
        }
        if(error.isEmpty())
          for(quint64 lane = start; lane < next; lane += 4)
          {
            uint32_t bits = field.value().value(lane);
            if(reset)
              memcpy(&bits, restored.data() + size_t(lane - start), 4);
            accepted[lane] = bits;
          }
      }
    }
    generation = r->GetSharedBufferPatchGeneration();
    replayEvent.Restore();
    QVariantMap timing;
    timing[QStringLiteral("inputWaitMs")] = inputWaitMS;
    timing[QStringLiteral("queueMs")] = queueMS;
    timing[QStringLiteral("readbackMs")] = readbackMS;
    timing[QStringLiteral("readbackBytes")] = qulonglong(readbackBytes);
    timing[QStringLiteral("mutationMs")] = mutationMS;
    timing[QStringLiteral("validationMs")] =
        operationClock.nsecsElapsed() / 1.0e6 - readbackMS - mutationMS;
    timing[QStringLiteral("nativeChanges")] = nativeChanges;
    timing[QStringLiteral("inputSamples")] = inputSamples;
    QElapsedTimer publicationClock;
    publicationClock.start();
    if(!guard)
      return;
    GUIInvoke::call(guard, [guard, epoch, request, publicationEvent, generation, error, externalChange,
                            discardCachedValues,
                            accepted, timing, publicationClock, oldestInput,
                            newestInput]() {
      if(!guard || !guard->m_CBufferLiveEdit)
        return;
      CBufferLiveEdit &live = *guard->m_CBufferLiveEdit;
      if(epoch->load() != request)
      {
        live.inFlight = false;
        live.panel->setProperty("busy", false);
        // An edit can finish just before a pin/capture-view change cancels its UI result.
        // Publish only the current replay generation, never the stale editor selection.
        guard->m_Ctx.RefreshReplayStatus(publicationEvent, generation, [](bool) {});
        guard->UI_SubmitCBufferLiveValues();
        return;
      }
      live.generation = generation;
      live.generationKnown = true;
      if(discardCachedValues)
        live.currentBits.clear();
      if(!error.isEmpty())
      {
        // A rejected value is not a pending preview. Drop queued repeats and restore the
        // numeric controls from the actual replay data after its publication completes.
        live.pending.clear();
        live.resets.clear();
        live.oldestInput.invalidate();
        live.newestInput.invalidate();
        live.queuedSamples = 0;
        live.panel->setProperty("pending", false);
      }
      if(externalChange)
      {
        live.currentBits.clear();
        live.pending.clear();
        live.panel->setProperty("pending", false);
        live.resets.clear();
        live.offsets.clear();
        live.key.clear();
        for(QWidget *channel : live.channels)
          channel->hide();
        live.color->hide();
        live.reset->setEnabled(false);
      }
      else
      {
        for(auto it = accepted.begin(); it != accepted.end(); ++it)
          live.currentBits[it.key()] = it.value();
      }
      live.panel->setProperty("error", error);
      live.panel->setProperty("generation", qulonglong(generation));
      live.status->setText(error.isEmpty() ? tr("Edits affect all uses of this shared buffer "
                                                "range. No shader compilation is needed.")
                                           : CBufferLiveErrorText(error));
      live.status->setToolTip(error);
      live.publishing = true;
      guard->m_Ctx.RefreshReplayStatus(publicationEvent, generation, [guard, epoch, request, error, timing,
          publicationClock, oldestInput, newestInput](bool published) {
        if(!guard || !guard->m_CBufferLiveEdit)
          return;
        CBufferLiveEdit &live = *guard->m_CBufferLiveEdit;
        live.publishing = false;
        live.inFlight = false;
        live.panel->setProperty("busy", false);
        QVariantMap completedTiming = timing;
        completedTiming[QStringLiteral("publicationMs")] = publicationClock.nsecsElapsed() / 1.0e6;
        completedTiming[QStringLiteral("oldestInputMs")] =
            oldestInput.isValid() ? oldestInput.nsecsElapsed() / 1.0e6 : 0.0;
        completedTiming[QStringLiteral("latestInputMs")] =
            newestInput.isValid() ? newestInput.nsecsElapsed() / 1.0e6 : 0.0;
        completedTiming[QStringLiteral("published")] = published;
        live.panel->setProperty("lastTiming", completedTiming);
        if(epoch->load() != request)
        {
          guard->UI_SubmitCBufferLiveValues();
          return;
        }
        live.reset->setEnabled(!live.offsets.isEmpty());
        if(guard->m_CBufferPinnedReflection && guard->m_CBufferPinnedEvent != guard->m_Ctx.CurEvent())
          guard->OnEventChanged(guard->m_CBufferPinnedEvent);
        else if(!error.isEmpty() || live.pending.isEmpty())
          guard->UI_RestoreCBufferLiveSelection();
        guard->UI_SubmitCBufferLiveValues();
      });
    });
  });
}

namespace
{
enum class UVTool
{
  Point,
  Edge,
  Face,
  Box,
  All,
  Linked,
  Clear,
  Focus,
  Flip
};

QIcon UVToolIcon(UVTool tool, const QPalette &palette)
{
  QPixmap image(40, 40);
  image.setDevicePixelRatio(2);
  image.fill(Qt::transparent);
  QPainter painter(&image);
  painter.setRenderHint(QPainter::Antialiasing);
  const QColor color = palette.color(QPalette::ButtonText);
  painter.setPen(QPen(color, 1.4, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
  const QPointF a(4, 15), b(10, 4), c(16, 15);
  switch(tool)
  {
    case UVTool::Point:
    case UVTool::Edge:
    case UVTool::Face:
    {
      QPolygonF triangle;
      triangle << a << b << c;
      if(tool == UVTool::Face)
        painter.setBrush(color);
      painter.drawPolygon(triangle);
      if(tool == UVTool::Point)
      {
        painter.setBrush(color);
        painter.drawEllipse(b, 2, 2);
      }
      else if(tool == UVTool::Edge)
      {
        painter.setPen(QPen(color, 3, Qt::SolidLine, Qt::RoundCap));
        painter.drawLine(a, c);
      }
      break;
    }
    case UVTool::Box:
      painter.setPen(QPen(color, 1.4, Qt::DashLine));
      painter.drawRect(QRectF(3, 3, 14, 14));
      break;
    case UVTool::All:
      painter.drawRect(QRectF(3, 3, 14, 14));
      painter.setBrush(color);
      painter.drawRect(QRectF(6, 6, 8, 8));
      break;
    case UVTool::Linked:
      painter.drawEllipse(QRectF(3, 7, 8, 6));
      painter.drawEllipse(QRectF(9, 7, 8, 6));
      break;
    case UVTool::Clear:
      painter.drawLine(QPointF(5, 5), QPointF(15, 15));
      painter.drawLine(QPointF(15, 5), QPointF(5, 15));
      break;
    case UVTool::Focus:
      painter.drawEllipse(QPointF(10, 10), 5, 5);
      painter.drawLine(QPointF(10, 2), QPointF(10, 6));
      painter.drawLine(QPointF(10, 14), QPointF(10, 18));
      painter.drawLine(QPointF(2, 10), QPointF(6, 10));
      painter.drawLine(QPointF(14, 10), QPointF(18, 10));
      break;
    case UVTool::Flip:
      painter.drawLine(QPointF(4, 10), QPointF(16, 10));
      painter.drawLine(QPointF(10, 3), QPointF(10, 17));
      painter.drawLine(QPointF(7, 6), QPointF(10, 3));
      painter.drawLine(QPointF(13, 6), QPointF(10, 3));
      painter.drawLine(QPointF(7, 14), QPointF(10, 17));
      painter.drawLine(QPointF(13, 14), QPointF(10, 17));
      break;
  }
  painter.end();
  return QIcon(image);
}
}

struct MeshUVHighlight
{
  rdcarray<FloatVector> positions;
  uint32_t selectedVertices = 0;
  Topology topology = Topology::TriangleList;
};

void BufferViewer::SetupMeshUV()
{
  m_UVData = std::make_shared<MeshUVData>();
  m_MeshSplit = new QSplitter(Qt::Horizontal, ui->renderContainer);
  m_MeshSplit->setObjectName(QStringLiteral("meshUVSplitter"));
  m_MeshSplit->setOpaqueResize(false);
  m_MeshSplit->setChildrenCollapsible(false);
  m_MeshStack = new QStackedWidget(m_MeshSplit);
  ui->renderContainerGridLayout->removeWidget(ui->render);
  m_MeshStack->addWidget(ui->render);
  m_MeshSplit->addWidget(m_MeshStack);

  m_UVMeshBox = new QRubberBand(QRubberBand::Rectangle, m_MeshStack);
  m_UVPanel = new QWidget(m_MeshSplit);
  QVBoxLayout *panel = new QVBoxLayout(m_UVPanel);
  panel->setContentsMargins(4, 0, 0, 0);
  panel->setSpacing(3);
  m_UVChannels = new QComboBox(m_UVPanel);
  m_UVChannels->setObjectName(QStringLiteral("meshUVChannels"));
  m_UVChannels->setToolTip(tr("UV channel of the selected mesh stage"));
  m_UVChannels->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
  panel->addWidget(m_UVChannels);
  m_UVTextures = new QComboBox(m_UVPanel);
  m_UVTextures->setObjectName(QStringLiteral("meshUVTextures"));
  m_UVTextures->setToolTip(tr("Texture bound as an input at this EID"));
  m_UVTextures->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
  QHBoxLayout *textureRow = new QHBoxLayout;
  textureRow->setSpacing(3);
  textureRow->addWidget(m_UVTextures, 1);
  QToolButton *channels = new QToolButton(m_UVPanel);
  // i18n-ignore: stable native control identifier and graphics channel identifiers.
  channels->setObjectName(QLatin1String("meshUVTextureChannels"));
  channels->setText(QLatin1String("RGB"));
  channels->setPopupMode(QToolButton::InstantPopup);
  channels->setToolTip(
      tr("Choose display channels. A single channel is shown in grayscale. For combinations, "
         "disabled RGB becomes 0 and disabled alpha becomes 1."));
  QMenu *menu = new QMenu(channels);
  QWidgetAction *channelOptions = new QWidgetAction(menu);
  QWidget *options = new QWidget(menu);
  QVBoxLayout *optionLayout = new QVBoxLayout(options);
  optionLayout->setContentsMargins(8, 5, 8, 5);
  // i18n-ignore: RGBA is a graphics channel identifier.
  QCheckBox *all = new QCheckBox(QLatin1String("RGBA"), options);
  all->setObjectName(QLatin1String("meshUVChannelAll"));
  optionLayout->addWidget(all);
  connect(all, &QCheckBox::toggled, this,
          [this](bool checked) { SetMeshUVTextureChannels(checked ? 15U : 0U); });
  for(int i = 0; i < 4; ++i)
  {
    // i18n-ignore: R/G/B/A are API channel identifiers.
    const QString name = QLatin1String("RGBA").mid(i, 1);
    QCheckBox *check = new QCheckBox(name, options);
    check->setObjectName(QLatin1String("meshUVChannel") + name);
    check->setChecked(i < 3);
    optionLayout->addWidget(check);
    connect(check, &QCheckBox::toggled, this, [this, i](bool checked) {
      SetMeshUVTextureChannels(checked ? m_UVData->textureChannels | (1U << i)
                                       : m_UVData->textureChannels & ~(1U << i));
    });
  }
  channelOptions->setDefaultWidget(options);
  menu->addAction(channelOptions);
  channels->setMenu(menu);
  textureRow->addWidget(channels);
  panel->addLayout(textureRow);

  m_UVCanvas = new MeshUVView(m_UVData, m_UVPanel);
  m_UVCanvas->setObjectName(QStringLiteral("meshUVCanvas"));
  m_UVCanvas->setToolTip(tr("Space: toggle texture tiling. Wheel: zoom. Middle-drag: pan."));
  panel->addWidget(m_UVCanvas, 1);

  QWidget *tools = new QWidget(ui->renderContainer);
  tools->setObjectName(QStringLiteral("meshComponentToolbar"));
  QHBoxLayout *selection = new QHBoxLayout(tools);
  selection->setContentsMargins(0, 0, 0, 0);
  selection->setSpacing(2);
  auto tool = [tools, selection](UVTool icon, const QString &tip, const char *id) {
    QToolButton *button = new QToolButton(tools);
    button->setIcon(UVToolIcon(icon, button->palette()));
    button->setIconSize(QSize(20, 20));
    button->setToolTip(tip);
    button->setAccessibleName(tip);
    button->setObjectName(QLatin1String(id));
    selection->addWidget(button);
    return button;
  };
  QButtonGroup *modes = new QButtonGroup(tools);
  const QString names[] = {tr("Select points (1)"), tr("Select edges (2)"), tr("Select faces (3)")};
  const char *ids[] = {"meshUVPoint", "meshUVEdge", "meshUVFace"};
  for(int i = 0; i < 3; ++i)
  {
    QToolButton *button = tool(UVTool(i), names[i], ids[i]);
    button->setCheckable(true);
    button->setChecked(i == int(m_UVData->mode));
    modes->addButton(button, i);
  }
  connect(modes, QOverload<int>::of(&QButtonGroup::buttonClicked), this,
          [this](int mode) { m_UVCanvas->setSelectionMode(MeshUVData::Selection(mode)); });
  m_UVCanvas->modeShortcut = [modes](int mode) { modes->button(mode)->click(); };

  selection->addSpacing(4);
  m_UVBox = tool(UVTool::Box, tr("Box select (B)"), "meshUVBox");
  m_UVBox->setCheckable(true);
  connect(m_UVBox, &QToolButton::toggled, m_UVCanvas, &MeshUVView::setBoxSelect);
  m_UVCanvas->boxShortcut = [this]() { m_UVBox->toggle(); };
  connect(tool(UVTool::All, tr("Select all (A)"), "meshUVAll"), &QToolButton::clicked, m_UVCanvas,
          &MeshUVView::selectAll);
  connect(tool(UVTool::Linked, tr("Select linked (L)"), "meshUVLinked"), &QToolButton::clicked,
          m_UVCanvas, &MeshUVView::selectLinked);
  connect(tool(UVTool::Clear, tr("Clear selection (Alt+A)"), "meshUVClear"), &QToolButton::clicked,
          m_UVCanvas, &MeshUVView::clearSelection);
  connect(tool(UVTool::Focus, tr("Focus selection (F)"), "meshUVFocus"), &QToolButton::clicked,
          this, [this]() { FocusMeshSelection(); });
  QToolButton *flip =
      tool(UVTool::Flip, tr("Flip the UV background and mesh texture sampling"), "meshUVFlip");
  flip->setCheckable(true);
  selection->addStretch();
  m_UVStatus = new QLabel(m_UVPanel);
  m_UVStatus->setWordWrap(true);
  panel->addWidget(m_UVStatus);
  m_MeshSplit->addWidget(m_UVPanel);
  m_MeshSplit->setStretchFactor(0, 2);
  m_MeshSplit->setStretchFactor(1, 1);
  ui->renderContainerGridLayout->removeWidget(ui->configurationGroup);
  ui->renderContainerGridLayout->addWidget(tools, 1, 0, 1, 2);
  ui->renderContainerGridLayout->addWidget(ui->configurationGroup, 2, 0);
  ui->renderContainerGridLayout->addWidget(m_MeshSplit, 2, 1);
  ui->renderContainerGridLayout->setRowStretch(2, 1);

  m_UVToggle = new QToolButton(ui->controlsToolbar);
  m_UVToggle->setText(tr("UV"));
  m_UVToggle->setObjectName(QStringLiteral("meshUVToggle"));
  m_UVToggle->setCheckable(true);
  m_UVToggle->setToolTip(tr("Show the UV viewer beside the mesh"));
  ui->controlsToolbar->layout()->addWidget(m_UVToggle);
  m_CameraPreset = new QComboBox(ui->controlsToolbar);
  m_CameraPreset->addItems({tr("Bread"), tr("3ds Max"), tr("Maya")});
  m_CameraPreset->setToolTip(tr("Mesh camera navigation preset"));
  m_CameraPreset->setCurrentIndex(qBound(0, m_Ctx.Config().MeshViewer_CameraNavigation, 2));
  ui->controlsToolbar->layout()->addWidget(m_CameraPreset);
  connect(m_CameraPreset, QOverload<int>::of(&QComboBox::activated), this, [this](int preset) {
    m_Ctx.Config().MeshViewer_CameraNavigation = preset;
    m_Ctx.Config().Save();
    if(preset != 0)
      ui->controlType->setCurrentIndex(0);
  });
  ui->visualisation->addItem(tr("UV texture"), QStringLiteral("mesh-uv"));
  connect(m_UVToggle, &QToolButton::toggled, this, [this, tools](bool visible) {
    m_UVPanel->setVisible(visible);
    tools->setVisible(true);
    if(visible)
    {
      m_MeshSplit->setSizes({600, 360});
      UpdateMeshUV();
    }
    else
    {
      if(ui->visualisation->currentIndex() == UVTextureMode())
        ui->visualisation->setCurrentIndex(int(m_Config.visualisationMode));
    }
    UpdateMeshUVPreview();
  });
  connect(m_UVChannels, QOverload<int>::of(&QComboBox::currentIndexChanged), this,
          &BufferViewer::UpdateMeshUVGeometry);
  connect(m_UVChannels, QOverload<int>::of(&QComboBox::activated), this, [this](int) {
    m_UVManualChannel = m_UVChannels->currentData(Qt::UserRole + 1).toString();
  });
  connect(m_UVTextures, QOverload<int>::of(&QComboBox::currentIndexChanged), this, [this]() {
    m_UVExplanationTexture = QImage();
    m_MeshEvidenceRequest.remove(QLatin1String("previewTexture"));
    m_UVTextures->setItemText(0, tr("No texture"));
    UpdateMeshUVTexture();
  });
  connect(flip, &QToolButton::toggled, this, [this](bool on) {
    m_UVData->flipTexture = on;
    m_UVCanvas->textureChanged();
    UpdateMeshUVPreview();
  });
  m_UVCanvas->meshPosition = [this](const MeshUVGeometry::Vertex &vertex) {
    return MeshPreviewPosition(vertex.position, vertex.index);
  };
  m_UVCanvas->selectionChanged = [this, modes]() {
    modes->button(int(m_UVData->mode))->setChecked(true);
    UpdateMeshUVSelection();
    if(!m_UVSelectionUpdating)
      SyncMeshUVTableSelection();
  };
  qApp->installEventFilter(this);
  m_UVToggle->setChecked(true);

  ui->cameraFov->setValue(qBound(ui->cameraFov->minimum(), m_Ctx.Config().MeshViewer_CameraFov,
                                 ui->cameraFov->maximum()));
  const auto updateFov = [this](int fov) {
    const float previous = qBound(1, m_Ctx.Config().MeshViewer_CameraFov, 130);
    const float ratio =
        std::tan(qDegreesToRadians(previous * .5f)) / std::tan(qDegreesToRadians(fov * .5f));
    m_Arcball->DollyZoom(ratio);
    m_Flycam->DollyZoom(ratio);
    m_Ctx.Config().MeshViewer_CameraFov = fov;
    ui->cameraFovValue->setText(tr("%1°").arg(fov));
    RENDERDOC_SetMeshPreviewFov(m_Arcball->camera(), float(fov));
    RENDERDOC_SetMeshPreviewFov(m_Flycam->camera(), float(fov));
    QueueMeshPreview();
  };
  updateFov(ui->cameraFov->value());
  connect(ui->cameraFov, &QSlider::valueChanged, this, updateFov);
}

int BufferViewer::UVTextureMode() const
{
  return ui->visualisation->findData(QStringLiteral("mesh-uv"));
}

void BufferViewer::NavigateMeshUV(int stage, const QString &channel, int component, ResourceId texture)
{
  if(!m_UVToggle || stage < 0 || stage >= ui->outputTabs->count())
    return;
  m_UVLinkPending = true;
  m_UVManualChannel.clear();
  m_UVLinkChannel = channel;
  m_UVLinkComponent = component;
  m_UVLinkTexture = texture;
  ui->outputTabs->setCurrentIndex(stage);
  if(m_UVToggle->isChecked())
    UpdateMeshUV();
  else
    m_UVToggle->setChecked(true);
}

// i18n-ignore: Mesh evidence JSON keys and component enum values are stable protocol identifiers.
void BufferViewer::NavigateMeshSelection(const QString &mode, const QVariantList &vertices,
                                         const QVariantList &elements, const QString &packed)
{
  // Navigation may arrive while the stage's geometry is still being decoded.
  m_MeshEvidenceRequest[QLatin1String("selectionMode")] = mode;
  m_MeshEvidenceRequest[QLatin1String("selectedVertices")] = vertices;
  m_MeshEvidenceRequest[QLatin1String("selectedElements")] = elements;
  m_MeshEvidenceRequest[QLatin1String("selectionKeyData")] = packed;
  m_MeshEvidenceSelectionPending = true;
  if(!m_UVData || !m_UVData->geometry || m_UVGeometryRunning || m_UVDataSequence != m_Sequence)
    return;
  const int selectedMode = mode == QLatin1String("point")  ? 0
                           : mode == QLatin1String("edge") ? 1
                                                           : 2;
  QSet<uint32_t> ids;
  for(const QVariant &value : vertices)
    ids.insert(value.toUInt());
  QSet<quint64> keys;
  if(!packed.isEmpty())
  {
    const QByteArray compressed = QByteArray::fromBase64(packed.mid(3).toLatin1());
    quint32 size = 0;
    for(int i = 0; i < qMin(4, compressed.size()); ++i)
      size = (size << 8) | quint8(compressed[i]);
    QByteArray raw;
    if(packed.startsWith(QLatin1String("v1:")) && compressed.size() >= 4 &&
       size <= 64 * 1024 * 1024 && size % 8 == 0)
      raw = qUncompress(compressed);
    if(raw.size() != qint64(size) || raw.isEmpty())
    {
      m_MeshEvidenceRequest[QLatin1String("selectionError")] =
          QLatin1String("invalid-packed-selection");
      m_MeshEvidenceSelectionPending = false;
      return;
    }
    QDataStream stream(raw);
    stream.setByteOrder(QDataStream::LittleEndian);
    while(!stream.atEnd())
    {
      quint64 value;
      stream >> value;
      keys.insert(value);
    }
  }
  else
    for(const QVariant &value : elements)
      keys.insert(value.toString().toULongLong());
  m_UVCanvas->modeShortcut(selectedMode);
  if(elements.isEmpty() && packed.isEmpty())
    m_UVCanvas->selectVertices(ids);
  else
    m_UVCanvas->selectElements(keys);
  m_MeshEvidenceSelectionPending = false;
}

void BufferViewer::SetMeshUVTextureChannels(uint32_t mask)
{
  m_UVData->textureChannels = mask & 15U;
  QString channelLabel;
  for(int i = 0; i < 4; ++i)
  {
    const QString channel =
        QLatin1String("RGBA").mid(i, 1);    // i18n-ignore: API channel identifier.
    if(m_UVData->textureChannels & (1U << i))
      channelLabel += channel;
    if(QCheckBox *check = findChild<QCheckBox *>(QLatin1String("meshUVChannel") + channel))
    {
      const QSignalBlocker blocker(check);
      check->setChecked((m_UVData->textureChannels & (1U << i)) != 0);
    }
  }
  if(QToolButton *channels = findChild<QToolButton *>(QLatin1String("meshUVTextureChannels")))
    channels->setText(channelLabel.isEmpty() ? tr("None") : channelLabel);
  m_UVCanvas->setProperty("uvTextureChannelMask", m_UVData->textureChannels);
  if(QCheckBox *all = findChild<QCheckBox *>(QLatin1String("meshUVChannelAll")))
  {
    const QSignalBlocker blocker(all);
    all->setChecked(m_UVData->textureChannels == 15U);
  }
  m_UVCanvas->textureChanged();
  if(m_UVTextureView)
    m_UVTextureView->requestUpdate();
}

void BufferViewer::setMeshEvidence(const QVariantMap &request)
{
  m_MeshEvidenceRequest = request;
  const QJsonObject preview =
      QJsonObject::fromVariantMap(request.value(QLatin1String("previewTexture")).toMap());
  m_UVExplanationTexture =
      AIVisualReply::verifiedImage(preview.value(QLatin1String("asset")).toObject());
  if(!preview.isEmpty() && m_UVExplanationTexture.isNull())
    m_MeshEvidenceRequest[QLatin1String("selectionError")] =
        QLatin1String("invalid-preview-texture");
  SetMeshUVTextureChannels(request.value(QLatin1String("textureChannelMask"), 7).toUInt());
  m_MeshEvidencePrepared = false;
  const QString stage = request.value(QLatin1String("stage")).toString();
  const int index = stage == QLatin1String("vs-out") ? 1 : stage == QLatin1String("gs-out") ? 2 : 0;
  const qulonglong raw = request.value(QLatin1String("textureResourceId")).toULongLong();
  ResourceId texture;
  memcpy(&texture, &raw, sizeof(texture));
  NavigateMeshUV(index, request.value(QLatin1String("channel")).toString(),
                 request.value(QLatin1String("pair")).toInt() * 2, texture);
  if(request.contains(QLatin1String("selectedVertices")) ||
     request.contains(QLatin1String("selectedElements")) ||
     request.contains(QLatin1String("selectionKeyData")))
    NavigateMeshSelection(
        request.value(QLatin1String("selectionMode"), QLatin1String("point")).toString(),
        request.value(QLatin1String("selectedVertices")).toList(),
        request.value(QLatin1String("selectedElements")).toList(),
        request.value(QLatin1String("selectionKeyData")).toString());
}

QString BufferViewer::meshEvidenceRequest() const
{
  return QString::fromUtf8(
      QJsonDocument(QJsonObject::fromVariantMap(m_MeshEvidenceRequest)).toJson(QJsonDocument::Compact));
}

void BufferViewer::setMeshEvidenceRequest(const QString &request)
{
  setMeshEvidence(QJsonDocument::fromJson(request.toUtf8()).object().toVariantMap());
}

bool BufferViewer::PrepareMeshEvidence()
{
  if(m_MeshEvidenceRequest.contains(QLatin1String("selectionError")))
    return false;
  if(!m_MeshView || !m_UVData || !m_UVData->geometry || m_UVGeometryRunning || m_UVLinkPending ||
     m_MeshEvidenceSelectionPending || m_UVTextureReadySerial != m_UVTextureSerial ||
     m_UVDataSequence != m_Sequence || m_UVEvent != m_Ctx.CurEvent() ||
     m_MeshEvidenceRequest.value(QLatin1String("eventId")).toUInt() != m_Ctx.CurEvent())
    return false;
  if(m_MeshEvidencePrepared)
    return true;
  if(m_MeshEvidenceRequest.value(QLatin1String("selectionExpansion")).toString() ==
     QLatin1String("connected"))
    m_UVCanvas->selectLinked();
  const QVariantList uv = m_MeshEvidenceRequest.value(QLatin1String("uvBounds")).toList();
  if(uv.size() == 4)
    m_UVCanvas->selectUVRegion(
        QRectF(QPointF(uv[0].toDouble(), uv[1].toDouble()),
               QPointF(uv[2].toDouble(), uv[3].toDouble())),
        m_MeshEvidenceRequest.value(QLatin1String("selectionExpansion")).toString() ==
            QLatin1String("connected"));
  QString framing = m_MeshEvidenceRequest.value(QLatin1String("framing")).toString();
  if(framing.isEmpty())
    framing = m_MeshEvidenceRequest.value(QLatin1String("focus")).toBool()
                  ? QLatin1String("selection")
                  : QLatin1String("preserve");
  if(framing != QLatin1String("preserve"))
  {
    const QVariantList angles = m_MeshEvidenceRequest.value(QLatin1String("viewAngles")).toList();
    if(angles.size() == 2 || angles.size() == 3)
    {
      auto radians = [](double degrees) {
        return std::isfinite(degrees) ? float(qDegreesToRadians(std::remainder(degrees, 360.0)))
                                      : 0.0f;
      };
      m_CurrentCamera->Orient(radians(angles[0].toDouble()), radians(angles[1].toDouble()),
                              radians(angles.value(2).toDouble()));
    }
    else if(!m_UVData->selected.isEmpty())
    {
      float values[16];
      RENDERDOC_GetMeshPreviewModelTransform(m_Config, m_Ctx.APIProps().pipelineType, values);
      const QMatrix4x4 model = QMatrix4x4(values).transposed();
      const QSet<uint32_t> selected = m_UVData->selectedVertices();
      QVector3D normal, mean, localLo, localHi;
      QVector<QVector3D> world;
      world.reserve(m_UVData->geometry->vertices.size());
      for(const auto &vertex : m_UVData->geometry->vertices)
      {
        QVector4D p = vertex.position;
        if(!m_Config.position.unproject)
          p.setW(1);
        world.push_back((model * p).toVector3DAffine());
        mean += world.back();
        const QVector3D local = p.toVector3DAffine();
        if(world.size() == 1)
          localLo = localHi = local;
        for(int axis = 0; axis < 3; ++axis)
        {
          localLo[axis] = qMin(localLo[axis], local[axis]);
          localHi[axis] = qMax(localHi[axis], local[axis]);
        }
      }
      mean /= float(qMax(1, world.size()));
      QVector3D covariance[3];
      for(const auto &p : world)
      {
        const QVector3D d = p - mean;
        for(int axis = 0; axis < 3; ++axis)
          covariance[axis] += d * d[axis];
      }
      const QVector3D extent = localHi - localLo;
      int longest = 0;
      for(int axis = 1; axis < 3; ++axis)
        if(extent[axis] > extent[longest])
          longest = axis;
      QVector3D sourceUp;
      sourceUp[longest] = 1;
      QVector3D desiredUp = model.mapVector(sourceUp).normalized();
      for(int i = 0; i < 16; ++i)
        desiredUp = QVector3D(QVector3D::dotProduct(covariance[0], desiredUp),
                              QVector3D::dotProduct(covariance[1], desiredUp),
                              QVector3D::dotProduct(covariance[2], desiredUp))
                        .normalized();
      QVector<QVector3D> samples, feature;
      QVector3D featureCenter;
      for(int i = 0; i < world.size(); ++i)
        if(selected.contains(m_UVData->geometry->vertices[i].index))
        {
          feature.push_back(world[i]);
          featureCenter += world[i];
        }
      featureCenter /= float(qMax(1, feature.size()));
      float variance = 0;
      for(const auto &p : feature)
        variance += (p - featureCenter).lengthSquared();
      for(const auto &face : m_UVData->geometry->triangles)
        if(selected.contains(m_UVData->geometry->vertices[face[0]].index) &&
           selected.contains(m_UVData->geometry->vertices[face[1]].index) &&
           selected.contains(m_UVData->geometry->vertices[face[2]].index))
        {
          normal += QVector3D::crossProduct(world[face[1]] - world[face[0]],
                                            world[face[2]] - world[face[0]]);
          samples.push_back((world[face[0]] + world[face[1]] + world[face[2]]) / 3);
        }
      normal.normalize();
      if(!normal.isNull())
      {
        float best = -FLT_MAX, bestYaw = 0, bestPitch = 0, bestRoll = 0;
        for(int y = 0; y < 8; ++y)
          for(int p = -3; p <= 3; ++p)
            for(int r = 0; r < 4; ++r)
            {
              const float yaw = qDegreesToRadians(y * 45.0f), pitch = qDegreesToRadians(p * 30.0f),
                          roll = qDegreesToRadians(r * 90.0f);
              m_CurrentCamera->Orient(yaw, pitch, roll);
              const FloatVector forward = m_CurrentCamera->camera()->GetForward();
              const FloatVector rawRight = m_CurrentCamera->camera()->GetRight(),
                                rawUp = m_CurrentCamera->camera()->GetUp();
              const QVector3D f(forward.x, forward.y, forward.z),
                  right(rawRight.x, rawRight.y, rawRight.z), up(rawUp.x, rawUp.y, rawUp.z);
              float hiddenVariance = 0;
              for(const auto &point : feature)
              {
                const float depth = QVector3D::dotProduct(point - featureCenter, f);
                hiddenVariance += depth * depth;
              }
              const float wholeVariance = covariance[0].x() + covariance[1].y() + covariance[2].z();
              const QVector3D projectedCovariance(QVector3D::dotProduct(covariance[0], f),
                                                  QVector3D::dotProduct(covariance[1], f),
                                                  QVector3D::dotProduct(covariance[2], f));
              const float wholeSpread =
                  1.0f - QVector3D::dotProduct(f, projectedCovariance) / qMax(1e-12f, wholeVariance);
              const float basisScore = 1.0f - hiddenVariance / qMax(1e-12f, variance) +
                                       2.5f * wholeSpread +
                                       .8f * QVector3D::dotProduct(desiredUp, up) -
                                       .1f * QVector3D::dotProduct(normal, f);
              if(basisScore + .5f <= best)
                continue;
              int visible = 0;
              const int sampleCount = qMin(3, samples.size());
              const float epsilon = qMax(1e-7f, extent.length() * 1e-4f);
              for(int sample = 0; sample < sampleCount; ++sample)
              {
                const QVector3D target =
                    samples[sample * (samples.size() - 1) / qMax(1, sampleCount - 1)];
                bool blocked = false;
                for(const auto &face : m_UVData->geometry->triangles)
                {
                  const QVector3D a = world[face[0]], ab = world[face[1]] - a,
                                  ac = world[face[2]] - a, q = target - a;
                  const float bx = QVector3D::dotProduct(ab, right),
                              by = QVector3D::dotProduct(ab, up),
                              cx = QVector3D::dotProduct(ac, right),
                              cy = QVector3D::dotProduct(ac, up),
                              qx = QVector3D::dotProduct(q, right),
                              qy = QVector3D::dotProduct(q, up), determinant = bx * cy - by * cx;
                  if(qAbs(determinant) < 1e-12f)
                    continue;
                  const float b = (qx * cy - qy * cx) / determinant,
                              c = (bx * qy - by * qx) / determinant;
                  if(b >= 0 && c >= 0 && b + c <= 1 &&
                     QVector3D::dotProduct(a + b * ab + c * ac - target, f) < -epsilon)
                  {
                    blocked = true;
                    break;
                  }
                }
                visible += !blocked;
              }
              const float score = .5f * float(visible) / float(qMax(1, sampleCount)) + basisScore;
              if(score > best)
              {
                best = score;
                bestYaw = yaw;
                bestPitch = pitch;
                bestRoll = roll;
              }
            }
        m_CurrentCamera->Orient(bestYaw, bestPitch, bestRoll);
        m_MeshEvidenceRequest[QLatin1String("viewAngles")] = QVariantList{
            qRadiansToDegrees(bestYaw), qRadiansToDegrees(bestPitch), qRadiansToDegrees(bestRoll)};
      }
    }
    FocusMeshSelection(framing == QLatin1String("context"));
    if(framing == QLatin1String("context"))
      m_UVCanvas->frameOverview();
    else
      m_UVCanvas->focusSelection();
  }
  m_MeshEvidencePrepared = true;
  return true;
}

QVariantMap BufferViewer::meshEvidence()
{
  QVariantMap result = m_MeshEvidenceRequest;
  result[QLatin1String("ready")] = false;
  if(!PrepareMeshEvidence())
    return result;
  result = m_MeshEvidenceRequest;
  UpdateMeshUVPreview();
  QImage mesh;
  if(m_UVTextureView && m_UVTextureContainer && m_UVTextureContainer->isVisible())
    mesh = m_UVTextureView->snapshot();
  else if(m_Output)
    m_Ctx.Replay().BlockInvoke([this, &mesh](IReplayController *controller) {
      const auto native = m_Output->GetDimensions();
      const double scale = qMax(1.0, qMin(4.0, 2048.0 / qMax(1, qMax(native.first, native.second))));
      IReplayOutput *evidence = controller->CreateOutput(
          CreateHeadlessWindowingData(int(native.first * scale), int(native.second * scale)),
          ReplayOutputType::Mesh);
      IReplayOutput *output = evidence ? evidence : m_Output;
      if(evidence)
      {
        MeshDisplay config = m_Config;
        config.cam = m_CurrentCamera->camera();
        const auto highlight = std::atomic_load(&m_UVHighlight);
        if(!config.wireframeDraw || (highlight && highlight->selectedVertices > 0))
          config.highlightVert = ~0U;
        if(config.wireframeDraw && highlight && !highlight->positions.empty())
          RENDERDOC_SetMeshComponentOverlay(evidence, highlight->positions.data(),
                                            uint32_t(highlight->positions.size()),
                                            highlight->topology, highlight->selectedVertices);
        evidence->SetMeshDisplay(config);
      }
      output->Display();
      const auto size = output->GetDimensions();
      const bytebuf pixels = output->ReadbackOutputTexture();
      if(size.first > 0 && size.second > 0 &&
         pixels.size() == size_t(size.first) * size_t(size.second) * 3)
        mesh =
            QImage(pixels.data(), size.first, size.second, size.first * 3, QImage::Format_RGB888).copy();
      if(evidence)
        evidence->Shutdown();
    });
  auto encode = [](QImage image) {
    image = image.scaled(QSize(1200, 1200).boundedTo(image.size()), Qt::KeepAspectRatio,
                         Qt::SmoothTransformation);
    QByteArray bytes;
    QBuffer buffer(&bytes);
    buffer.open(QIODevice::WriteOnly);
    image.save(&buffer, "PNG");
    return QString::fromLatin1(bytes.toBase64());
  };
  if(mesh.isNull())
    return result;
  auto boundsValue = [](const QRectF &bounds) {
    return QVariantList{bounds.left(), bounds.top(), bounds.right(), bounds.bottom()};
  };
  QImage uv = m_UVCanvas->snapshot();
  QRectF uvSelection = m_UVCanvas->selectionImageBounds();
  if(m_MeshEvidenceRequest.value(QLatin1String("framing")).toString() == QLatin1String("context"))
  {
    const QRectF crop = m_UVCanvas->overviewImageBounds();
    const QRect pixels = QRectF(crop.x() * uv.width(), crop.y() * uv.height(),
                                crop.width() * uv.width(), crop.height() * uv.height())
                             .toAlignedRect()
                             .intersected(uv.rect());
    if(pixels.width() >= 16 && pixels.height() >= 16)
    {
      const QRectF actual(double(pixels.x()) / uv.width(), double(pixels.y()) / uv.height(),
                          double(pixels.width()) / uv.width(), double(pixels.height()) / uv.height());
      uv = uv.copy(pixels);
      uvSelection =
          QRectF((uvSelection.x() - actual.x()) / actual.width(),
                 (uvSelection.y() - actual.y()) / actual.height(),
                 uvSelection.width() / actual.width(), uvSelection.height() / actual.height());
    }
  }
  result[QLatin1String("uvPng")] = encode(uv);
  result[QLatin1String("uvSelectionBounds")] = boundsValue(uvSelection);
  const QSet<uint32_t> selectedIndices = m_UVData->selectedVertices();
  const QMatrix4x4 projection = MeshPreviewTransform();
  QRectF selectedBounds, wholeBounds;
  bool firstPoint = true, firstWhole = true;
  for(const auto &vertex : m_UVData->geometry->vertices)
  {
    QVector4D v = MeshPreviewPosition(vertex.position, vertex.index);
    if(!m_Config.position.unproject)
      v.setW(1);
    v = projection * v;
    if(!std::isfinite(v.x()) || !std::isfinite(v.y()) || !std::isfinite(v.w()) || v.w() <= 0)
      continue;
    const QPointF p(v.x() / v.w() * .5 + .5, .5 - v.y() / v.w() * .5);
    if(firstWhole)
      wholeBounds = QRectF(p, QSizeF(0, 0));
    else
      wholeBounds =
          QRectF(QPointF(qMin(wholeBounds.left(), p.x()), qMin(wholeBounds.top(), p.y())),
                 QPointF(qMax(wholeBounds.right(), p.x()), qMax(wholeBounds.bottom(), p.y())));
    firstWhole = false;
    if(!selectedIndices.contains(vertex.index))
      continue;
    if(firstPoint)
      selectedBounds = QRectF(p, QSizeF(0, 0));
    else
      selectedBounds =
          QRectF(QPointF(qMin(selectedBounds.left(), p.x()), qMin(selectedBounds.top(), p.y())),
                 QPointF(qMax(selectedBounds.right(), p.x()), qMax(selectedBounds.bottom(), p.y())));
    firstPoint = false;
  }
  selectedBounds = selectedBounds.adjusted(-.015, -.015, .015, .015).intersected(QRectF(0, 0, 1, 1));
  if(!firstWhole &&
     m_MeshEvidenceRequest.value(QLatin1String("framing")).toString() == QLatin1String("context"))
  {
    const QRectF crop =
        wholeBounds.adjusted(-.035, -.035, .035, .035).intersected(QRectF(0, 0, 1, 1));
    const QRect pixels = QRectF(crop.x() * mesh.width(), crop.y() * mesh.height(),
                                crop.width() * mesh.width(), crop.height() * mesh.height())
                             .toAlignedRect()
                             .intersected(mesh.rect());
    if(pixels.width() >= 16 && pixels.height() >= 16)
    {
      const QRectF actual(double(pixels.x()) / mesh.width(), double(pixels.y()) / mesh.height(),
                          double(pixels.width()) / mesh.width(),
                          double(pixels.height()) / mesh.height());
      mesh = mesh.copy(pixels);
      selectedBounds =
          QRectF((selectedBounds.x() - actual.x()) / actual.width(),
                 (selectedBounds.y() - actual.y()) / actual.height(),
                 selectedBounds.width() / actual.width(), selectedBounds.height() / actual.height())
              .intersected(QRectF(0, 0, 1, 1));
    }
  }
  result[QLatin1String("meshPng")] = encode(mesh);
  result[QLatin1String("meshSelectionBounds")] =
      firstPoint ? QVariantList() : boundsValue(selectedBounds);
  result[QLatin1String("meshProjectedBounds")] =
      firstWhole ? QVariantList() : boundsValue(wholeBounds);
  result[QLatin1String("vertexCount")] = m_UVData->geometry->vertices.size();
  result[QLatin1String("triangleCount")] = m_UVData->geometry->triangles.size();
  const int channel = m_UVChannels->currentData().toInt();
  BufferItemModel *model = currentBufferModel();
  if(model && m_UVChannels->currentIndex() >= 0 && channel / 4 < model->getConfig().columns.count())
    result[QLatin1String("channel")] =
        QString::fromUtf8(model->getConfig().columns[channel / 4].name.c_str());
  result[QLatin1String("channelLabel")] = m_UVChannels->currentText();
  result[QLatin1String("pair")] = channel % 4 / 2;
  result[QLatin1String("stage")] = ui->outputTabs->currentIndex() == 0   ? QLatin1String("vs-in")
                                   : ui->outputTabs->currentIndex() == 1 ? QLatin1String("vs-out")
                                                                         : QLatin1String("gs-out");
  qulonglong textureResourceId = 0;
  memcpy(&textureResourceId, &m_UVTextureBinding.resource, sizeof(textureResourceId));
  result[QLatin1String("textureResourceId")] = QString::number(textureResourceId);
  const bool textured = textureResourceId || !m_UVExplanationTexture.isNull();
  result[QLatin1String("textureName")] = textured ? m_UVTextures->currentText() : QString();
  if(!m_UVExplanationTexture.isNull())
    result[QLatin1String("textureName")] = m_UVTextures->itemText(0);
  result[QLatin1String("textureProvenance")] =
      m_UVExplanationTexture.isNull() ? QLatin1String("captured") : QLatin1String("illustration");
  result[QLatin1String("textureChannelMask")] = m_UVData->textureChannels;
  result[QLatin1String("uvTextureRendered")] =
      textured && m_UVCanvas->property("uvTextureReady").toBool();
  result[QLatin1String("meshTextureRendered")] =
      textured && m_UVTextureContainer && m_UVTextureContainer->isVisible() &&
      m_UVTextureContainer->property("uvTextureRendered").toBool();
  result[QLatin1String("selectedCount")] = m_UVData->selected.size();
  QVariantList available;
  for(const auto &vertex : m_UVData->geometry->vertices)
    if(available.size() < 12)
      available.push_back(vertex.index);
  result[QLatin1String("availableVertices")] = available;
  const QSet<uint32_t> selected = m_UVData->selectedVertices();
  QVariantList indices;
  for(uint32_t index : selected)
    if(indices.size() < 4096)
      indices.push_back(index);
  result[QLatin1String("selectedVertices")] = indices;
  QVariantList elements;
  for(quint64 key : m_UVData->selected)
    if(elements.size() < 4096)
      elements.push_back(QString::number(key));
  result[QLatin1String("selectedElements")] = elements;
  result.remove(QLatin1String("selectionKeyData"));
  result[QLatin1String("selectionListComplete")] = m_UVData->selected.size() <= 4096;
  if(m_UVData->selected.size() > 4096)
  {
    QVector<quint64> ordered = m_UVData->selected.values().toVector();
    std::sort(ordered.begin(), ordered.end());
    QByteArray raw;
    QDataStream stream(&raw, QIODevice::WriteOnly);
    stream.setByteOrder(QDataStream::LittleEndian);
    for(quint64 key : ordered)
      stream << key;
    result[QLatin1String("selectionKeyData")] =
        QLatin1String("v1:") + QString::fromLatin1(qCompress(raw).toBase64());
  }
  result[QLatin1String("selectionComplete")] = true;
  result[QLatin1String("selectedVerticesComplete")] = selected.size() <= 4096;
  result[QLatin1String("selectionMode")] =
      m_UVData->mode == MeshUVData::Selection::Point  ? QLatin1String("point")
      : m_UVData->mode == MeshUVData::Selection::Edge ? QLatin1String("edge")
                                                      : QLatin1String("face");
  result[QLatin1String("ready")] = true;
  return result;
}

void BufferViewer::UpdateMeshUV()
{
  if(!m_UVToggle || m_UVEvent != m_Ctx.CurEvent() || m_UVDataSequence != m_Sequence)
    return;
  BufferItemModel *model = currentBufferModel();
  if(!model)
    return;
  const BufferConfiguration &config = model->getConfig();
  const QString previous = m_UVManualChannel;
  {
    const QSignalBlocker block(m_UVChannels);
    m_UVChannels->clear();
    int preferred = -1, best = -1;
    for(int column = 0; column < config.columns.count(); ++column)
    {
      const ShaderConstant &element = config.columns[column];
      const BufferElementProperties &prop = config.props[column];
      if(column == model->posColumn() || prop.perinstance || prop.perprimitive ||
         prop.systemValue != ShaderBuiltin::Undefined || element.type.columns < 2)
        continue;
      const QString name = QString::fromUtf8(element.name.c_str());
      const QString display = config.channelAliases.value(column, name);
      const QString lower = display.toLower();
      const bool named =
          lower.contains(QLatin1String("texcoord")) || lower.contains(QLatin1String("textoord")) ||
          QRegularExpression(QStringLiteral("(^|[_.])uv([0-9_.]|$)")).match(lower).hasMatch();
      const bool numericalCandidate =
          prop.format.compType != CompType::UInt && prop.format.compType != CompType::SInt &&
          !lower.contains(QLatin1String("normal")) && !lower.contains(QLatin1String("color")) &&
          !lower.contains(QLatin1String("tangent")) && !lower.contains(QLatin1String("weight"));
      for(int component = 0; component + 1 < int(element.type.columns); component += 2)
      {
        QString label = display;
        if(element.type.columns >= 4)
          label += component == 0 ? QLatin1String(".xy") : QLatin1String(".zw");
        int score = named ? 100 : 0;
        if(!named && numericalCandidate)
        {
          int inTile = 0, count = 0;
          QRectF range;
          for(uint32_t row = 0; row < config.numRows && count < 48;
              row += qMax(1U, config.numRows / 48))
          {
            const QVariantList values = model->meshValues(row, column);
            if(values.size() <= component + 1)
              continue;
            const double u = values[component].toDouble(), v = values[component + 1].toDouble();
            if(!std::isfinite(u) || !std::isfinite(v))
              continue;
            if(count == 0)
              range = QRectF(u, v, 0, 0);
            else
              range = QRectF(QPointF(qMin(u, range.left()), qMin(v, range.top())),
                             QPointF(qMax(u, range.right()), qMax(v, range.bottom())));
            ++count;
            if(u >= -.01 && u <= 1.01 && v >= -.01 && v <= 1.01)
              ++inTile;
          }
          if(count >= 3 && inTile * 10 >= count * 8 && range.width() > .02 && range.height() > .02)
          {
            score = element.type.columns == 2 ? 20 : 10;
            label = tr("%1 (UV candidate)").arg(label);
          }
        }
        m_UVChannels->addItem(label, column * 4 + component);
        const int index = m_UVChannels->count() - 1;
        m_UVChannels->setItemData(index, name + QLatin1Char(':') + QString::number(component),
                                  Qt::UserRole + 1);
        if((m_UVLinkPending && (name == m_UVLinkChannel || display == m_UVLinkChannel) &&
            component == m_UVLinkComponent) ||
           (!m_UVLinkPending &&
            m_UVChannels->itemData(index, Qt::UserRole + 1).toString() == previous))
          preferred = index;
        if(score > best)
        {
          best = score;
          m_UVChannels->setCurrentIndex(index);
        }
      }
    }
    if(preferred >= 0)
    {
      m_UVChannels->setCurrentIndex(preferred);
      if(m_UVLinkPending)
        m_UVManualChannel = m_UVChannels->currentData(Qt::UserRole + 1).toString();
    }
  }
  const ResourceId previousTexture =
      m_UVTextures->currentIndex() > 0 && m_UVTextures->currentIndex() <= m_UVTextureBindings.count()
          ? m_UVTextureBindings[m_UVTextures->currentIndex() - 1].resource
          : ResourceId();
  {
    const QSignalBlocker block(m_UVTextures);
    m_UVTextures->clear();
    m_UVTextures->addItem(tr("No texture"));
    m_UVTextureBindings.clear();
    const PipeState &pipe = m_Ctx.CurPipelineState();
    for(ShaderStage stage : {ShaderStage::Pixel, ShaderStage::Vertex, ShaderStage::Geometry,
                             ShaderStage::Hull, ShaderStage::Domain, ShaderStage::Mesh})
    {
      for(const UsedDescriptor &binding : pipe.GetReadOnlyResources(stage, true))
      {
        const Descriptor &descriptor = binding.descriptor;
        const TextureDescription *texture = m_Ctx.GetTexture(descriptor.resource);
        if(!texture || texture->dimension != 2 || texture->msSamp != 1 ||
           uint64_t(texture->width) * texture->height > 32U * 1024U * 1024U)
          continue;
        bool seen = false;
        for(const Descriptor &entry : m_UVTextureBindings)
          seen |= entry.resource == descriptor.resource && entry.firstMip == descriptor.firstMip &&
                  entry.firstSlice == descriptor.firstSlice;
        if(seen)
          continue;
        m_UVTextureBindings.push_back(descriptor);
        m_UVTextures->addItem(m_Ctx.GetResourceName(descriptor.resource));
        if(descriptor.resource == (m_UVLinkPending ? m_UVLinkTexture : previousTexture))
          m_UVTextures->setCurrentIndex(m_UVTextures->count() - 1);
      }
    }
  }
  if(m_UVLinkPending)
  {
    if(m_UVTextures->currentIndex() > 0 || !m_UVExplanationTexture.isNull())
      ui->visualisation->setCurrentIndex(UVTextureMode());
    m_UVLinkPending = false;
  }
  UpdateMeshUVGeometry();
  UpdateMeshUVTexture();
}

void BufferViewer::UpdateMeshUVGeometry()
{
  if(!m_UVData)
    return;
  const QScopedValueRollback<bool> updating(m_UVSelectionUpdating, true);
  ++m_UVGeometrySerial;
  m_UVData->geometry.reset();
  m_UVCanvas->geometryChanged();
  m_UVStatus->clear();
  StartMeshUVGeometry();
}

void BufferViewer::StartMeshUVGeometry()
{
  BufferItemModel *model = currentBufferModel();
  if(m_UVGeometryRunning || !model || m_UVEvent != m_Ctx.CurEvent())
    return;
  const BufferConfiguration &config = model->getConfig();
  if(config.topology != Topology::TriangleList && config.topology != Topology::TriangleList_Adj &&
     config.topology != Topology::TriangleStrip && config.topology != Topology::TriangleFan)
    return;

  auto snapshot = std::make_shared<BufferConfiguration>();
  *snapshot = config;
  const int channel = m_UVChannels->currentIndex() < 0 ? -1 : m_UVChannels->currentData().toInt();
  const int positionColumn = model->posColumn();
  const uint32_t serial = m_UVGeometrySerial;
  const uint32_t count = config.numRows;
  m_UVGeometryRunning = true;
  QPointer<BufferViewer> self(this);
  LambdaThread *thread = new LambdaThread([self, snapshot, channel, positionColumn, serial, count]() {
    auto mesh = std::make_shared<MeshUVGeometry>();
    mesh->vertices.reserve(int(count));
    QHash<uint32_t, int> decoded;
    QVector<int> run;
    const int listSize = snapshot->topology == Topology::TriangleList_Adj ? 6
                         : snapshot->topology == Topology::TriangleList   ? 3
                                                                          : 0;
    for(uint32_t row = 0; row < count; ++row)
    {
      if(listSize == 6 && row % 2 == 1)
        continue;
      if(listSize > 0 && row % listSize == 0)
        run.clear();
      const uint32_t index = MeshIndex(*snapshot, row);
      if(index == ~0U)
      {
        run.clear();
        continue;
      }
      int vertex = decoded.value(index, -1);
      if(vertex < 0)
      {
        const QVariantList coords =
            channel >= 0 ? MeshValues(*snapshot, index, channel / 4) : QVariantList();
        const QVariantList pos = MeshValues(*snapshot, index, positionColumn);
        if(pos.size() < 2)
        {
          run.clear();
          continue;
        }
        const bool hasUV = channel >= 0 && coords.size() > channel % 4 + 1;
        const QPointF candidate =
            hasUV ? QPointF(coords[channel % 4].toDouble(), coords[channel % 4 + 1].toDouble())
                  : QPointF();
        const bool validUV = hasUV && std::isfinite(candidate.x()) && std::isfinite(candidate.y()) &&
                             std::abs(candidate.x()) <= 1e6 && std::abs(candidate.y()) <= 1e6;
        const QPointF uv = validUV ? candidate : QPointF();
        const QVector4D position(pos[0].toFloat(), pos[1].toFloat(), pos.value(2).toFloat(),
                                 pos.size() >= 4 ? pos[3].toFloat() : 1.0f);
        if(!std::isfinite(position.x()) || !std::isfinite(position.y()) ||
           !std::isfinite(position.z()) || !std::isfinite(position.w()))
        {
          run.clear();
          continue;
        }
        vertex = mesh->vertices.size();
        decoded.insert(index, vertex);
        mesh->vertices.push_back({position, uv, index, validUV});
      }
      run.push_back(vertex);
      if(run.size() < 3)
        continue;
      const int a = snapshot->topology == Topology::TriangleFan ? run.first() : run[run.size() - 3];
      const int b = run[run.size() - 2], c = run.last();
      if(a != b && a != c && b != c)
      {
        mesh->triangles.push_back({{a, b, c}});
        if(mesh->vertices[a].uvValid && mesh->vertices[b].uvValid && mesh->vertices[c].uvValid)
        {
          mesh->wire.moveTo(mesh->vertices[a].uv);
          mesh->wire.lineTo(mesh->vertices[b].uv);
          mesh->wire.lineTo(mesh->vertices[c].uv);
          mesh->wire.closeSubpath();
        }
      }
      if(listSize > 0)
        run.clear();
    }
    mesh->bounds = mesh->wire.boundingRect().adjusted(-1e-6, -1e-6, 1e-6, 1e-6);
    GUIInvoke::call(qApp, [self, mesh, serial]() {
      if(!self)
        return;
      self->m_UVGeometryRunning = false;
      if(self->m_UVGeometrySerial != serial)
      {
        self->StartMeshUVGeometry();
        return;
      }
      self->m_UVData->geometry = mesh;
      {
        const QScopedValueRollback<bool> updating(self->m_UVSelectionUpdating, true);
        self->m_UVCanvas->geometryChanged();
        BufferItemModel *model = self->currentBufferModel();
        QSet<uint32_t> vertices;
        const bool paired =
            (self->m_CurStage == MeshDataStage::VSIn || self->m_CurStage == MeshDataStage::VSOut) &&
            (self->m_UVSelectionStage == MeshDataStage::VSIn ||
             self->m_UVSelectionStage == MeshDataStage::VSOut);
        if(model && (paired || self->m_CurStage == self->m_UVSelectionStage))
          for(int row : self->m_UVSelectedRows)
            if(row >= 0 && uint32_t(row) < model->getConfig().numRows)
              vertices.insert(model->meshIndex(uint32_t(row)));
        self->m_UVCanvas->selectVertices(vertices);
      }
      self->SyncMeshUVTableSelection();
      if(self->m_MeshEvidenceSelectionPending)
        self->NavigateMeshSelection(
            self->m_MeshEvidenceRequest.value(QLatin1String("selectionMode")).toString(),
            self->m_MeshEvidenceRequest.value(QLatin1String("selectedVertices")).toList(),
            self->m_MeshEvidenceRequest.value(QLatin1String("selectedElements")).toList(),
            self->m_MeshEvidenceRequest.value(QLatin1String("selectionKeyData")).toString());
      self->m_UVCanvas->resetView();
      if(!self->m_MeshCameraInitialised)
        self->FocusMeshSelection(true);
      self->PrepareMeshEvidence();
      self->UpdateMeshUVPreview();
    });
  });
  thread->selfDelete(true);
  thread->start();
}

void BufferViewer::UpdateMeshUVTexture()
{
  if(!m_UVExplanationTexture.isNull() &&
     m_MeshEvidenceRequest.value(QLatin1String("eventId")).toUInt() != m_UVEvent)
  {
    m_UVExplanationTexture = QImage();
    m_MeshEvidenceRequest.remove(QLatin1String("previewTexture"));
    m_UVTextures->setItemText(0, tr("No texture"));
  }
  if(!m_UVExplanationTexture.isNull())
  {
    ++m_UVTextureSerial;
    m_UVTextureReadySerial = m_UVTextureSerial;
    m_UVTextureBinding = Descriptor();
    m_UVTextureEvent = m_UVEvent;
    m_UVData->texture = m_UVExplanationTexture;
    const QString label = m_MeshEvidenceRequest.value(QLatin1String("previewTexture"))
                              .toMap()
                              .value(QLatin1String("label"))
                              .toString();
    m_UVTextures->setItemText(0, label.isEmpty() ? tr("Explanation texture") : label);
    m_UVCanvas->textureChanged();
    PrepareMeshEvidence();
    UpdateMeshUVPreview();
    return;
  }
  const int index = m_UVTextures->currentIndex() - 1;
  Descriptor descriptor;
  if(index >= 0 && index < m_UVTextureBindings.count())
    descriptor = m_UVTextureBindings[index];
  const uint32_t event = m_UVEvent;
  if(event == m_UVTextureEvent && descriptor == m_UVTextureBinding)
    return;
  m_UVTextureEvent = event;
  m_UVTextureBinding = descriptor;
  const uint32_t serial = ++m_UVTextureSerial;
  const TextureDescription *texture = m_Ctx.GetTexture(descriptor.resource);
  if(!texture || event != m_Ctx.CurEvent())
  {
    m_UVTextureReadySerial = serial;
    m_UVData->texture = QImage();
    m_UVCanvas->textureChanged();
    UpdateMeshUVPreview();
    return;
  }

  // Keep the last completed texture until the replacement is ready. A thumbnail request
  // never changes the native child window, camera or geometry of the current preview.
  const uint32_t mip = qMin(uint32_t(descriptor.firstMip), 31U);
  const QSize source(qMax(1U, texture->width >> mip), qMax(1U, texture->height >> mip));
  const QSize size =
      source.scaled(QSize(1024, 1024), Qt::KeepAspectRatio).boundedTo(source).expandedTo(QSize(1, 1));
  const bool hasAlpha = texture->format.compCount == 4;
  QPointer<BufferViewer> self(this);
  const rdcstr tag = QFormatStr("meshUVTexture-%1").arg(uintptr_t(this));
  m_Ctx.Replay().AsyncInvoke(tag, [self, descriptor, serial, event, size,
                                   hasAlpha](IReplayController *r) {
    if(!self || self->m_UVTextureSerial != serial)
      return;
    QImage image;
    if(r->GetCurrentEventID() == event)
    {
      if(!self->m_UVThumbnailOutput)
        self->m_UVThumbnailOutput = r->CreateOutput(WindowingData(), ReplayOutputType::Texture);
      if(self->m_UVThumbnailOutput)
      {
        const bytebuf pixels = self->m_UVThumbnailOutput->DrawThumbnail(
            size.width(), size.height(), descriptor.resource,
            Subresource(descriptor.firstMip, descriptor.firstSlice, 0), descriptor.format.compType);
        if(pixels.size() == size_t(size.width()) * size_t(size.height()) * 3)
          image = QImage(pixels.data(), size.width(), size.height(), size.width() * 3,
                         QImage::Format_RGB888)
                      .copy();
        if(!image.isNull() && hasAlpha)
        {
          IReplayOutput *alphaOutput = r->CreateOutput(
              CreateHeadlessWindowingData(size.width(), size.height()), ReplayOutputType::Texture);
          if(alphaOutput)
          {
            TextureDisplay display;
            display.resourceId = descriptor.resource;
            display.subresource = Subresource(descriptor.firstMip, descriptor.firstSlice, 0);
            display.typeCast = descriptor.format.compType;
            display.red = display.green = display.blue = false;
            display.alpha = true;
            display.scale = -1.0f;
            alphaOutput->SetTextureDisplay(display);
            alphaOutput->Display();
            const bytebuf alpha = alphaOutput->ReadbackOutputTexture();
            alphaOutput->Shutdown();
            if(alpha.size() == size_t(size.width()) * size.height() * 3)
            {
              image = image.convertToFormat(QImage::Format_ARGB32);
              for(int y = 0; y < size.height(); ++y)
              {
                QRgb *row = reinterpret_cast<QRgb *>(image.scanLine(y));
                for(int x = 0; x < size.width(); ++x)
                  row[x] = qRgba(qRed(row[x]), qGreen(row[x]), qBlue(row[x]),
                                 alpha[(size_t(y) * size.width() + x) * 3]);
              }
            }
          }
        }
      }
    }
    GUIInvoke::call(qApp, [self, image, serial, event]() {
      if(!self || self->m_UVTextureSerial != serial || self->m_UVEvent != event)
        return;
      self->m_UVData->texture = image;
      self->m_UVTextureReadySerial = serial;
      self->m_UVCanvas->textureChanged();
      self->PrepareMeshEvidence();
      self->UpdateMeshUVPreview();
    });
  });
}

QMatrix4x4 BufferViewer::MeshPreviewTransform() const
{
  MeshDisplay config = m_Config;
  config.cam = m_CurrentCamera->camera();
  float matrix[16];
  const QSize size = m_MeshStack->size();
  RENDERDOC_GetMeshPreviewTransform(config, m_Ctx.APIProps().pipelineType,
                                    float(qMax(1, size.width())) / qMax(1, size.height()), matrix);
  return QMatrix4x4(matrix).transposed();
}

QVector4D BufferViewer::MeshPreviewPosition(const QVector4D &position, uint32_t index) const
{
  if(ui->visualisation->currentIndex() != int(Visualisation::Explode) ||
     m_Config.exploderScale <= 0.0f)
    return position;

  // Match the native mesh vertex shader when drawing the component overlay.
  const auto &lo = m_Config.minBounds;
  const auto &hi = m_Config.maxBounds;
  const QVector3D center((lo.x + hi.x) * .5f, (lo.y + hi.y) * .5f, (lo.z + hi.z) * .5f);
  const QVector3D offset = position.toVector3D() - center;
  const float scale = m_Config.vtxExploderSliderSNorm;
  const float distance = 4.0f * m_Config.exploderScale * scale * scale * scale *
                         (float((index >> 1) & 15) / 10.0f - .5f);
  return position +
         QVector4D(offset * (distance / std::sqrt(qMax(offset.lengthSquared(), FLT_EPSILON))), 0.0f);
}

void BufferViewer::FocusMeshSelection(bool wholeMesh)
{
  if(!m_Ctx.IsCaptureLoaded() || !m_CurrentCamera)
    return;
  RecoverMeshCamera();

  float values[16];
  RENDERDOC_GetMeshPreviewModelTransform(m_Config, m_Ctx.APIProps().pipelineType, values);
  const QMatrix4x4 modelTransform = QMatrix4x4(values).transposed();
  for(float value : values)
    if(!std::isfinite(value))
      return;
  ICamera *camera = m_CurrentCamera->camera();
  const auto direction = [](const FloatVector &v) { return QVector3D(v.x, v.y, v.z); };
  const QVector3D right = direction(camera->GetRight());
  const QVector3D up = direction(camera->GetUp());
  const QVector3D forward = direction(camera->GetForward());
  QVector3D lo, hi;
  bool haveBounds = false;
  auto include = [&](QVector4D position) {
    if(!m_Config.position.unproject)
      position.setW(1.0f);
    const QVector4D world = modelTransform * position;
    if(world.w() == 0.0f)
      return;
    const QVector3D point = world.toVector3DAffine();
    const QVector3D p(QVector3D::dotProduct(point, right), QVector3D::dotProduct(point, up),
                      QVector3D::dotProduct(point, forward));
    if(!std::isfinite(p.x()) || !std::isfinite(p.y()) || !std::isfinite(p.z()))
      return;
    if(!haveBounds)
    {
      lo = hi = p;
      haveBounds = true;
    }
    for(int i = 0; i < 3; ++i)
    {
      lo[i] = qMin(lo[i], p[i]);
      hi[i] = qMax(hi[i], p[i]);
    }
  };

  const auto geometry = m_UVData ? m_UVData->geometry : nullptr;
  if(!wholeMesh && geometry && !m_UVData->selected.isEmpty())
  {
    const auto &mesh = *geometry;
    if(m_UVData->mode == MeshUVData::Selection::Point)
    {
      for(const auto &vertex : mesh.vertices)
        if(m_UVData->selected.contains(vertex.index))
          include(MeshPreviewPosition(vertex.position, vertex.index));
    }
    else
    {
      const int vertices = m_UVData->mode == MeshUVData::Selection::Face ? 3 : 2;
      for(int i = 0; i < mesh.triangles.size(); ++i)
        for(int corner = 0; corner < m_UVData->elementCount(); ++corner)
          if(m_UVData->selected.contains(m_UVData->key(i, corner)))
            for(int j = 0; j < vertices; ++j)
            {
              const auto &vertex = mesh.vertices[mesh.triangles[i][(corner + j) % 3]];
              include(MeshPreviewPosition(vertex.position, vertex.index));
            }
    }
  }
  if(!wholeMesh && !haveBounds && m_Config.highlightVert != ~0U)
  {
    BufferItemModel *model = currentBufferModel();
    if(model)
    {
      const QVariantList pos = model->meshValues(m_Config.highlightVert, model->posColumn());
      if(pos.size() >= (m_Config.position.unproject ? 4 : 2))
        include(QVector4D(pos[0].toFloat(), pos[1].toFloat(), pos.value(2).toFloat(),
                          pos.value(3).toFloat()));
    }
  }
  if(!haveBounds && geometry)
    for(const auto &vertex : geometry->vertices)
      include(MeshPreviewPosition(vertex.position, vertex.index));
  if(!haveBounds && m_Config.showBBox)
  {
    const auto &a = m_Config.minBounds;
    const auto &b = m_Config.maxBounds;
    for(int i = 0; i < 8; ++i)
      include(QVector4D(i & 1 ? b.x : a.x, i & 2 ? b.y : a.y, i & 4 ? b.z : a.z, 1.0f));
  }
  if(!haveBounds)
    return;

  // Split before adding/subtracting, so opposite finite limits cannot overflow.
  const QVector3D center = lo * .5f + hi * .5f;
  const QVector3D extent = hi * .5f - lo * .5f;
  const QSize size = m_MeshStack ? m_MeshStack->size() : ui->render->size();
  const float aspect = float(qMax(1, size.width())) / qMax(1, size.height());
  const float tangent = std::tan(qDegreesToRadians(float(m_Ctx.Config().MeshViewer_CameraFov) * .5f));
  // Frame the projected bounds with a small margin and leave the camera's rotation alone.
  const float distance =
      extent.isNull() ? 0.0f
                      : extent.z() + qMax(2.0f * m_Ctx.Config().MeshViewer_CameraNear,
                                          1.12f * qMax(extent.x() / aspect, extent.y()) / tangent);
  const QVector3D target = center.x() * right + center.y() * up + center.z() * forward;
  if(!std::isfinite(distance) || !std::isfinite(target.x()) || !std::isfinite(target.y()) ||
     !std::isfinite(target.z()))
    return;
  m_CurrentCamera->Frame(target, distance);
  m_MeshCameraInitialised = true;
  QueueMeshPreview();
}

bool BufferViewer::RecoverMeshCamera()
{
  if(!m_CurrentCamera)
    return false;
  ICamera *camera = m_CurrentCamera->camera();
  const FloatVector position = camera->GetPosition(), right = camera->GetRight(),
                    up = camera->GetUp(), forward = camera->GetForward();
  bool valid = true;
  for(const auto &v : {position, right, up, forward})
    valid = valid && std::isfinite(v.x) && std::isfinite(v.y) && std::isfinite(v.z);
  if(valid)
    return false;
  // A non-finite pivot contaminates the whole view basis. Framing cannot use
  // that basis to recover; reset only this invalid camera before calculating bounds.
  if(m_CurrentCamera == m_Arcball)
    m_Arcball->Reset(FloatVector(), 10.0f);
  else
    m_Flycam->Reset(FloatVector());
  camera->SetNearFar(m_Ctx.Config().MeshViewer_CameraNear, m_Ctx.Config().MeshViewer_CameraFar);
  RENDERDOC_SetMeshPreviewFov(camera, float(m_Ctx.Config().MeshViewer_CameraFov));
  m_MeshCameraInitialised = false;
  m_MeshClipValid = false;
  return true;
}

void BufferViewer::UpdateMeshCameraClip()
{
  if(!m_CurrentCamera || !m_UVData || !m_UVData->geometry)
    return;
  float values[16];
  RENDERDOC_GetMeshPreviewModelTransform(m_Config, m_Ctx.APIProps().pipelineType, values);
  const QMatrix4x4 model = QMatrix4x4(values).transposed();
  const auto geometry = m_UVData->geometry;
  if(geometry != m_MeshClipGeometry || model != m_MeshClipModel)
  {
    m_MeshClipGeometry = geometry;
    m_MeshClipModel = model;
    m_MeshClipValid = false;
    for(const auto &vertex : geometry->vertices)
    {
      QVector4D position = vertex.position;
      if(!m_Config.position.unproject)
        position.setW(1.0f);
      const QVector4D world = model * position;
      if(world.w() == 0.0f)
        continue;
      const QVector3D p = world.toVector3DAffine();
      if(!std::isfinite(p.x()) || !std::isfinite(p.y()) || !std::isfinite(p.z()))
        continue;
      if(!m_MeshClipValid)
      {
        m_MeshClipMin = m_MeshClipMax = p;
        m_MeshClipValid = true;
      }
      for(int component = 0; component < 3; ++component)
      {
        m_MeshClipMin[component] = qMin(m_MeshClipMin[component], p[component]);
        m_MeshClipMax[component] = qMax(m_MeshClipMax[component], p[component]);
      }
    }
  }
  if(!m_MeshClipValid)
    return;
  float nearest = FLT_MAX, furthest = -FLT_MAX;
  for(int corner = 0; corner < 8; ++corner)
  {
    const float depth =
        m_CurrentCamera->ViewDepth(QVector3D(corner & 1 ? m_MeshClipMax.x() : m_MeshClipMin.x(),
                                             corner & 2 ? m_MeshClipMax.y() : m_MeshClipMin.y(),
                                             corner & 4 ? m_MeshClipMax.z() : m_MeshClipMin.z()));
    nearest = qMin(nearest, depth);
    furthest = qMax(furthest, depth);
  }
  // Tighten the depth range around the visible mesh without clipping vertices close to the eye.
  const float margin =
      qMax((m_MeshClipMax - m_MeshClipMin).length() * .1f, m_Ctx.Config().MeshViewer_CameraNear * 4);
  const float slider = m_Config.vtxExploderSliderSNorm;
  const float displacement = m_Config.visualisationMode == Visualisation::Explode
                                 ? qAbs(m_Config.exploderScale) * 4 * slider * slider * slider
                                 : 0.0f;
  const float nearPlane = qMax(m_Ctx.Config().MeshViewer_CameraNear, (nearest - displacement) * .25f);
  const float farPlane = qMin(m_Ctx.Config().MeshViewer_CameraFar,
                              qMax(nearPlane * 2, furthest + margin + displacement));
  m_CurrentCamera->camera()->SetNearFar(nearPlane, farPlane);
}

void BufferViewer::UpdateMeshUVPreview()
{
  if(!m_MeshCameraRecovering && RecoverMeshCamera())
  {
    const QScopedValueRollback<bool> recovering(m_MeshCameraRecovering, true);
    FocusMeshSelection(true);
  }
  UpdateMeshCameraClip();
  // The selected visualisation owns the surface even when its next texture is pending.
  const bool textured = m_Ctx.IsCaptureLoaded() && m_UVToggle->isChecked() &&
                        ui->visualisation->currentIndex() == UVTextureMode();
  if(textured && !m_UVTextureView)
  {
    m_UVTextureView = new MeshUVTextureView(m_UVData);
    m_UVTextureContainer = QWidget::createWindowContainer(m_UVTextureView, m_MeshStack);
    m_UVTextureContainer->setObjectName(QStringLiteral("meshUVTextureView"));
    m_UVTextureContainer->setFocusPolicy(Qt::StrongFocus);
    m_UVTextureView->rendered = [this](qint64 textureKey, bool wireframe) {
      m_UVTextureContainer->setProperty("uvTextureRendered", textureKey != 0);
      m_UVTextureContainer->setProperty("uvTextureKey", textureKey);
      m_UVTextureContainer->setProperty("uvWireframeRendered", wireframe);
    };
    m_UVTextureView->installEventFilter(this);
    m_UVTextureView->unavailable = [this]() {
      QTimer::singleShot(0, this, [this]() {
        if(ui->visualisation->currentIndex() == UVTextureMode())
          ui->visualisation->setCurrentIndex(int(m_Config.visualisationMode));
        m_UVStatus->setText(tr("The GPU texture preview is unavailable."));
      });
    };
    m_MeshStack->addWidget(m_UVTextureContainer);
    m_UVTextureView->setSelection(m_UVTexturedSelection, m_UVSelectionPrimitive);
  }
  if(textured && m_MeshStack->currentWidget() != m_UVTextureContainer)
    m_MeshStack->setCurrentWidget(m_UVTextureContainer);
  else if(!textured && m_MeshStack->currentWidget() != ui->render)
    m_MeshStack->setCurrentWidget(ui->render);
  if(textured)
    m_UVTextureView->setTransform(MeshPreviewTransform(), m_Config.position.unproject,
                                  m_Config.wireframeDraw, m_Ctx.Config().MeshViewer_CameraNear);
}

void BufferViewer::SyncMeshUVTableSelection()
{
  BufferItemModel *model = currentBufferModel();
  RDTableView *table = currentTable();
  if(!model || !table || !m_UVData->geometry)
    return;
  const QScopedValueRollback<bool> updating(m_UVSelectionUpdating, true);
  const QSet<uint32_t> vertices = m_UVData->selectedVertices();
  QItemSelection rows;
  int first = -1;
  m_UVSelectedRows.clear();
  m_UVSelectionStage = m_CurStage;
  const int count = int(model->getConfig().numRows);
  for(int row = 0; row <= count; ++row)
  {
    const bool selected = row < count && vertices.contains(model->meshIndex(uint32_t(row)));
    if(selected)
    {
      m_UVSelectedRows.insert(row);
      if(first < 0)
        first = row;
    }
    else if(first >= 0)
    {
      rows.select(model->index(first, 0), model->index(row - 1, model->columnCount() - 1));
      first = -1;
    }
  }
  {
    const QSignalBlocker block(table->selectionModel());
    table->selectionModel()->select(
        rows, QItemSelectionModel::ClearAndSelect | QItemSelectionModel::Rows);
    if(!rows.empty())
    {
      table->selectionModel()->setCurrentIndex(rows.first().topLeft(), QItemSelectionModel::NoUpdate);
      table->scrollTo(rows.first().topLeft(), QAbstractItemView::EnsureVisible);
    }
  }
  m_CurView = table;
  m_CurFixed = false;
  UpdateHighlightVerts();
  SyncViews(table, true, false);
}

void BufferViewer::UpdateMeshUVSelection()
{
  auto highlight = std::make_shared<MeshUVHighlight>();
  QVector<QVector4D> texturedSelection;
  const auto mode = m_UVData->mode;
  const int count = mode == MeshUVData::Selection::Face   ? 3
                    : mode == MeshUVData::Selection::Edge ? 2
                                                          : 1;
  const unsigned int glPrimitive = count == 3 ? GL_TRIANGLES : count == 2 ? GL_LINES : GL_POINTS;
  highlight->topology = count == 3   ? Topology::TriangleList
                        : count == 2 ? Topology::LineList
                                     : Topology::PointList;
  if(m_UVData->geometry)
  {
    const auto &mesh = *m_UVData->geometry;
    if(mode == MeshUVData::Selection::Point)
    {
      auto appendPoint = [&](const QVector4D &p) {
        const FloatVector v(p.x(), p.y(), p.z(), p.w());
        for(int i = 0; i < 6; ++i)
          highlight->positions.push_back(v);
      };
      for(const auto &vertex : mesh.vertices)
      {
        if(m_UVData->selected.contains(vertex.index))
        {
          texturedSelection.push_back(vertex.position);
          appendPoint(MeshPreviewPosition(vertex.position, vertex.index));
        }
      }
      highlight->selectedVertices = uint32_t(highlight->positions.size());
      for(const auto &vertex : mesh.vertices)
        if(!m_UVData->selected.contains(vertex.index))
          appendPoint(MeshPreviewPosition(vertex.position, vertex.index));
    }
    else if(!m_UVData->selected.isEmpty())
    {
      QSet<quint64> included;
      for(int i = 0; i < mesh.triangles.size(); ++i)
      {
        const auto &face = mesh.triangles[i];
        for(int corner = 0; corner < m_UVData->elementCount(); ++corner)
        {
          const quint64 key = m_UVData->key(i, corner);
          if(!m_UVData->selected.contains(key) || included.contains(key))
            continue;
          included.insert(key);
          for(int vertex = 0; vertex < count; ++vertex)
          {
            const auto &v = mesh.vertices[face[(corner + vertex) % 3]];
            texturedSelection.push_back(v.position);
            const QVector4D p = MeshPreviewPosition(v.position, v.index);
            highlight->positions.push_back(FloatVector(p.x(), p.y(), p.z(), p.w()));
          }
        }
      }
      highlight->selectedVertices = uint32_t(highlight->positions.size());
    }
  }
  ui->render->setProperty("uvHighlightedVertices", int(highlight->selectedVertices));
  ui->render->setProperty("uvPointMarkers",
                          mode == MeshUVData::Selection::Point
                              ? int((highlight->positions.size() - highlight->selectedVertices) / 6)
                              : 0);
  m_UVTexturedSelection = texturedSelection;
  m_UVSelectionPrimitive = glPrimitive;
  if(m_UVTextureView)
    m_UVTextureView->setSelection(m_UVTexturedSelection, m_UVSelectionPrimitive);
  std::atomic_store(&m_UVHighlight, std::shared_ptr<const MeshUVHighlight>(highlight));
  QueueMeshPreview();
}

void BufferViewer::QueueMeshPreview()
{
  if(!m_Ctx.IsCaptureLoaded())
    return;
  if(!m_MeshCameraRecovering && RecoverMeshCamera())
  {
    const QScopedValueRollback<bool> recovering(m_MeshCameraRecovering, true);
    FocusMeshSelection(true);
  }
  UpdateMeshCameraClip();
  if(m_UVTextureView && m_MeshStack->currentWidget() == m_UVTextureContainer)
  {
    m_UVTextureView->setTransform(MeshPreviewTransform(), m_Config.position.unproject,
                                  m_Config.wireframeDraw, m_Ctx.Config().MeshViewer_CameraNear);
    return;
  }
  const rdcstr tag = QFormatStr("mesh-preview-%1").arg(uintptr_t(this));
  QPointer<BufferViewer> self(this);
  m_Ctx.Replay().AsyncInvoke(tag, [self](IReplayController *r) {
    if(self)
      self->RT_UpdateAndDisplay(r);
  });
}

void BufferViewer::SetupMeshView()
{
  // hide buttons we don't want in the toolbar
  ui->byteRangeLine->setVisible(false);
  ui->byteRangeStartLabel->setVisible(false);
  byteRangeStart->setVisible(false);
  ui->byteRangeLengthLabel->setVisible(false);
  byteRangeLength->setVisible(false);

  ui->meshFilterLabel->setVisible(false);
  ui->resetMeshFilterButton->setVisible(false);

  ui->fixedVars->setVisible(false);
  ui->showPadding->setVisible(false);

  ui->fixedVars->setColumns({tr("Name"), tr("Value"), tr("Type")});
  {
    ui->fixedVars->header()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
    ui->fixedVars->header()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
  }

  ui->fixedVars->setFont(Formatter::FixedFont());

  ui->resourceDetails->setVisible(false);
  ui->formatSpecifier->setVisible(false);
  ui->configurationGroup->setVisible(false);

  ui->minBoundsLabel->setText(lit("---"));
  ui->maxBoundsLabel->setText(lit("---"));

  ui->outputTabs->setWindowTitle(tr("Preview"));
  ui->dockarea->addToolWindow(ui->outputTabs, ToolWindowManager::EmptySpace);
  ui->dockarea->setToolWindowProperties(ui->outputTabs, ToolWindowManager::HideCloseButton);

  ui->inTable->setFrameShape(QFrame::NoFrame);
  ui->dockarea->addToolWindow(
      m_Containers[0], ToolWindowManager::AreaReference(
                           ToolWindowManager::TopOf, ui->dockarea->areaOf(ui->outputTabs), 0.5f));
  ui->dockarea->setToolWindowProperties(m_Containers[0], ToolWindowManager::HideCloseButton);

  ui->out1Table->setFrameShape(QFrame::NoFrame);
  ui->dockarea->addToolWindow(
      m_Containers[1], ToolWindowManager::AreaReference(
                           ToolWindowManager::RightOf, ui->dockarea->areaOf(m_Containers[0]), 0.5f));
  ui->dockarea->setToolWindowProperties(m_Containers[1], ToolWindowManager::HideCloseButton);

  ui->out2Table->setFrameShape(QFrame::NoFrame);
  ui->dockarea->addToolWindow(
      m_Containers[2], ToolWindowManager::AreaReference(
                           ToolWindowManager::AddTo, ui->dockarea->areaOf(m_Containers[1]), 0.5f));
  ui->dockarea->setToolWindowProperties(m_Containers[2], ToolWindowManager::HideCloseButton);

  ToolWindowManager::raiseToolWindow(m_Containers[1]);

  updateLabelsAndLayout();

  m_HeaderMenu = new QMenu(this);

  m_ResetColumnSel = new QAction(tr("Reset Selected Columns"), this);
  m_SelectPosColumn = new QAction(tr("Select as Position"), this);
  m_SelectSecondColumn = new QAction(tr("Select as Secondary"), this);
  m_SelectSecondAlphaColumn = new QAction(tr("Select Alpha as Secondary"), this);

  m_HeaderMenu->addAction(m_ResetColumnSel);
  m_HeaderMenu->addSeparator();
  m_HeaderMenu->addAction(m_SelectPosColumn);
  m_HeaderMenu->addAction(m_SelectSecondColumn);
  m_HeaderMenu->addAction(m_SelectSecondAlphaColumn);

  QObject::connect(m_ResetColumnSel, &QAction::triggered, [this]() {
    BufferItemModel *model = (BufferItemModel *)m_CurView->model();

    model->setPosColumn(-1);
    model->setSecondaryColumn(-1, m_Config.visualisationMode == Visualisation::Secondary, false);

    UI_ConfigureFormats();
    UpdateCurrentMeshConfig();
    UpdateMeshUVGeometry();
    INVOKE_MEMFN(RT_UpdateAndDisplay);
  });
  QObject::connect(m_SelectPosColumn, &QAction::triggered, [this]() {
    BufferItemModel *model = (BufferItemModel *)m_CurView->model();

    model->setPosColumn(m_ContextColumn);

    UI_ConfigureFormats();
    UpdateCurrentMeshConfig();
    UpdateMeshUVGeometry();
    INVOKE_MEMFN(RT_UpdateAndDisplay);
  });
  QObject::connect(m_SelectSecondColumn, &QAction::triggered, [this]() {
    BufferItemModel *model = (BufferItemModel *)m_CurView->model();

    model->setSecondaryColumn(m_ContextColumn,
                              m_Config.visualisationMode == Visualisation::Secondary, false);

    UI_ConfigureFormats();
    UpdateCurrentMeshConfig();
    INVOKE_MEMFN(RT_UpdateAndDisplay);
  });
  QObject::connect(m_SelectSecondAlphaColumn, &QAction::triggered, [this]() {
    BufferItemModel *model = (BufferItemModel *)m_CurView->model();

    model->setSecondaryColumn(m_ContextColumn,
                              m_Config.visualisationMode == Visualisation::Secondary, true);
    UI_ConfigureFormats();
    UpdateCurrentMeshConfig();
    INVOKE_MEMFN(RT_UpdateAndDisplay);
  });

  ui->inTable->horizontalHeader()->setContextMenuPolicy(Qt::CustomContextMenu);
  ui->out1Table->horizontalHeader()->setContextMenuPolicy(Qt::CustomContextMenu);
  ui->out2Table->horizontalHeader()->setContextMenuPolicy(Qt::CustomContextMenu);

  ui->inTable->horizontalHeader()->setTextElideMode(Qt::ElideMiddle);
  ui->out1Table->horizontalHeader()->setTextElideMode(Qt::ElideMiddle);
  ui->out2Table->horizontalHeader()->setTextElideMode(Qt::ElideMiddle);

  ui->inTable->setPinnedColumns(2);
  ui->out1Table->setPinnedColumns(2);
  ui->out2Table->setPinnedColumns(2);

  ui->inTable->setColumnGroupRole(columnGroupRole);
  ui->out1Table->setColumnGroupRole(columnGroupRole);
  ui->out2Table->setColumnGroupRole(columnGroupRole);

  QObject::connect(ui->inTable->horizontalHeader(), &QHeaderView::customContextMenuRequested,
                   [this](const QPoint &pos) { meshHeaderMenu(MeshDataStage::VSIn, pos); });
  QObject::connect(ui->out1Table->horizontalHeader(), &QHeaderView::customContextMenuRequested,
                   [this](const QPoint &pos) { meshHeaderMenu(MeshDataStage::VSOut, pos); });
  QObject::connect(ui->out2Table->horizontalHeader(), &QHeaderView::customContextMenuRequested,
                   [this](const QPoint &pos) { meshHeaderMenu(MeshDataStage::GSOut, pos); });

  QVBoxLayout *vertical = new QVBoxLayout(this);

  vertical->setSpacing(3);
  vertical->setContentsMargins(3, 3, 3, 3);

  vertical->addWidget(ui->meshToolbar);
  vertical->addWidget(ui->dockarea);

  QTimer *renderTimer = new QTimer(this);
  QObject::connect(renderTimer, &QTimer::timeout, this, &BufferViewer::render_timer);
  renderTimer->setSingleShot(false);
  renderTimer->setInterval(10);
  renderTimer->start();
}

void BufferViewer::meshHeaderMenu(MeshDataStage stage, const QPoint &pos)
{
  int col = tableForStage(stage)->horizontalHeader()->logicalIndexAt(pos);

  if(col < 2)
    return;

  m_CurView = tableForStage(stage);
  m_CurFixed = false;
  m_ContextColumn = modelForStage(stage)->elementIndexForColumn(col);

  bool perPrim = modelForStage(stage)->propForColumn(col).perprimitive;

  if(perPrim)
  {
    m_SelectPosColumn->setEnabled(false);
    m_SelectSecondColumn->setEnabled(false);
    m_SelectSecondAlphaColumn->setEnabled(false);
  }
  else
  {
    m_SelectPosColumn->setEnabled(true);
    m_SelectSecondColumn->setEnabled(true);

    m_SelectSecondAlphaColumn->setEnabled(modelForStage(stage)->elementForColumn(col).type.columns ==
                                          4);
  }

  m_HeaderMenu->popup(tableForStage(stage)->horizontalHeader()->mapToGlobal(pos));
}

void BufferViewer::fixedVars_contextMenu(const QPoint &pos)
{
  RDTreeWidgetItem *item = ui->fixedVars->itemAt(pos);
  FixedVarTag tag;
  if(item)
    tag = item->tag().value<FixedVarTag>();

  m_CurView = NULL;
  m_CurFixed = true;

  updateExportActionNames();

  QMenu contextMenu(this);

  QAction expandAll(tr("&Expand All"), this);
  QAction collapseAll(tr("C&ollapse All"), this);
  QAction copy(tr("&Copy"), this);
  QAction showPadding(tr("&Show Padding"), this);
  QAction removeFilter(tr("&Remove Filter"), this);
  QAction filterTask(tr("&Filter to this Task"), this);
  QAction gotoMesh(tr("&Go to meshes"), this);

  expandAll.setIcon(Icons::arrow_out());
  collapseAll.setIcon(Icons::arrow_in());
  copy.setIcon(Icons::copy());
  removeFilter.setIcon(Icons::arrow_undo());
  filterTask.setIcon(Icons::filter());
  gotoMesh.setIcon(Icons::arrow_join());
  showPadding.setCheckable(true);
  showPadding.setChecked(ui->showPadding->isChecked());

  expandAll.setEnabled(item && item->childCount() > 0);
  removeFilter.setEnabled(item && m_CurMeshFilter != MeshFilter::None);
  filterTask.setEnabled(item);
  gotoMesh.setEnabled(item);
  collapseAll.setEnabled(expandAll.isEnabled());

  contextMenu.addAction(&expandAll);
  contextMenu.addAction(&collapseAll);
  contextMenu.addAction(&copy);

  contextMenu.addSeparator();

  int idx = ui->fixedVars->indexOfTopLevelItem(item);

  if(m_MeshView)
  {
    contextMenu.addAction(&removeFilter);
    contextMenu.addAction(&filterTask);
    contextMenu.addAction(&gotoMesh);

    const BufferConfiguration &config1 = m_ModelOut1->getConfig();

    // if we're already filtering to a task, don't offer to filter any more. However if we're
    // filtered to a mesh allow 'broadening' the filter back to the task
    // also don't allow filtering at all if there is no task shader bound
    filterTask.setEnabled(!config1.taskSizes.empty() && m_CurMeshFilter != MeshFilter::TaskGroup);

    if(config1.taskSizes.empty() ||
       config1.taskSizes[idx].x * config1.taskSizes[idx].y * config1.taskSizes[idx].z == 0)
      gotoMesh.setEnabled(false);

    // if there's a filter don't enable goto mesh as normally we just scroll to the first mesh - it
    // would be redundant and potentially annoying to be able to and doesn't do anything useful
    if(m_CurMeshFilter != MeshFilter::None)
      gotoMesh.setEnabled(false);
  }
  else
  {
    contextMenu.addAction(&showPadding);
  }

  contextMenu.addSeparator();

  contextMenu.addAction(m_ExportCSV);
  contextMenu.addAction(m_ExportBytes);

  QObject::connect(&removeFilter, &QAction::triggered, [this]() { SetMeshFilter(MeshFilter::None); });
  QObject::connect(&filterTask, &QAction::triggered, [this, idx]() {
    // if there's no filter, select this task. If we were mesh filtering, filter back to all meshes
    // under the current task (don't use idx there, since it will just be 0)
    if(m_CurMeshFilter == MeshFilter::None)
      SetMeshFilter(MeshFilter::TaskGroup, idx);
    else
      SetMeshFilter(MeshFilter::TaskGroup, m_FilteredTaskGroup);
  });
  QObject::connect(&gotoMesh, &QAction::triggered, [this, idx]() {
    const BufferConfiguration &config1 = m_ModelOut1->getConfig();

    uint32_t meshletIndex = 0;
    for(int i = 0; i < idx && i < config1.taskSizes.count(); i++)
    {
      meshletIndex += config1.taskSizes[i].x * config1.taskSizes[i].y * config1.taskSizes[i].z;
    }

    const BufferConfiguration &config2 = m_ModelOut2->getConfig();
    uint32_t vertexOffset = config2.meshletVertexPrefixCounts[meshletIndex];

    ShowMeshData(MeshDataStage::MeshOut);
    ScrollToRow((int)vertexOffset, MeshDataStage::MeshOut);
  });

  QObject::connect(&expandAll, &QAction::triggered,
                   [this, item]() { ui->fixedVars->expandAllItems(item); });

  QObject::connect(&collapseAll, &QAction::triggered,
                   [this, item]() { ui->fixedVars->collapseAllItems(item); });
  QObject::connect(&copy, &QAction::triggered,
                   [this, item, pos]() { ui->fixedVars->copyItem(pos, item); });
  QObject::connect(&showPadding, &QAction::triggered,
                   [this]() { ui->showPadding->setChecked(!ui->showPadding->isChecked()); });

  if(IsCBufferView() && m_CurCBuffer.valid && m_BufferID != ResourceId())
  {
    const ShaderReflection *reflection =
        CBufferReflection();
    if(reflection && m_CBufferSlot.slot < reflection->constantBlocks.size())
    {
      const ConstantBlock &block = reflection->constantBlocks[m_CBufferSlot.slot];
      QJsonObject attachment = ReplayLabAIResourceContext(m_Ctx, m_BufferID, "cbuffer-viewer");
      if(m_CBufferPinnedReflection)
      {
        QJsonObject capture = attachment[QLatin1String("capture")].toObject();
        capture[QLatin1String("eventId")] = int(CBufferEvent());
        attachment[QLatin1String("capture")] = capture;
        QJsonObject origin = attachment[QLatin1String("origin")].toObject();
        const QString identity = QString(m_Ctx.GetCaptureFilename()) + QLatin1Char('\n') +
                                 QString::number(CBufferEvent()) + QLatin1Char('\n') + ToQStr(m_BufferID);
        origin[QLatin1String("sourceId")] = QLatin1String("resource-") + QString::fromLatin1(
            QCryptographicHash::hash(identity.toUtf8(), QCryptographicHash::Sha256).toHex());
        attachment[QLatin1String("origin")] = origin;
      }
      QJsonObject resource = attachment[QLatin1String("resource")].toObject();
      resource[QLatin1String("byteOffset")] = QString::number(m_ByteOffset);
      resource[QLatin1String("byteSize")] = QString::number(block.byteSize);
      resource[QLatin1String("binding")] = int(block.fixedBindNumber);
      resource[QLatin1String("registerSpace")] = int(block.fixedBindSetOrSpace);
      resource[QLatin1String("blockIndex")] = int(m_CBufferSlot.slot);
      resource[QLatin1String("arrayIndex")] = int(m_CBufferSlot.arrayIdx);
      QJsonObject shader;
      shader[QLatin1String("stage")] = ToQStr(m_CBufferSlot.stage);
      shader[QLatin1String("resourceId")] = ToQStr(m_CurCBuffer.shader);
      shader[QLatin1String("entryPoint")] = QString(m_CurCBuffer.entryPoint);
      attachment[QLatin1String("shader")] = shader;
      attachment[QLatin1String("resource")] = resource;
      // The current pipeline belongs to the selected event, not necessarily this pinned
      // buffer. Its captured binding above remains sufficient to resolve the attachment.
      const QJsonObject blockDetails = CBufferEvent() == m_Ctx.CurEvent()
                                           ? ReplayLabAIResourceDetails(m_Ctx, m_BufferID, attachment)
                                           : attachment;
      const QString blockTitle = tr("CBuffer: %1 (EID %2)").arg(QString(block.name)).arg(CBufferEvent());
      const QString blockJSON = ReplayLabAIResourceJSON(attachment);
      const QString blockText = ReplayLabAIResourceJSON(blockDetails);
      contextMenu.addSeparator();
      QAction *addBlock = contextMenu.addAction(tr("Add CBuffer to AI conversation"));
      addBlock->setObjectName(QStringLiteral("addCBufferToAI"));
      QObject::connect(addBlock, &QAction::triggered, [this, blockTitle, blockText, blockJSON]() {
        m_Ctx.AddTextToAIAssistant(blockTitle, blockText, blockTitle, blockJSON);
      });
      if(item && CanAttachCBufferParameter(tag))
      {
        if(!tag.path.isEmpty())
          resource[QLatin1String("fieldPath")] = tag.path;
        resource[QLatin1String("fieldName")] = item->text(0);
        resource[QLatin1String("baseType")] = ToQStr(tag.replayLabBaseType);
        resource[QLatin1String("components")] = int(tag.replayLabComponents);
        QJsonArray offsets;
        for(quint64 offset : tag.replayLabEditOffsets)
          offsets.append(QString::number(offset));
        resource[QLatin1String("componentByteOffsets")] = offsets;
        attachment[QLatin1String("resource")] = resource;
        QJsonObject fieldDetails = blockDetails;
        fieldDetails[QLatin1String("resource")] = resource;
        fieldDetails[QLatin1String("displayedValue")] = item->text(1);
        fieldDetails[QLatin1String("capturedFieldName")] = QString(tag.name);
        fieldDetails[QLatin1String("meaningSource")] = tag.aliasSource;
        fieldDetails[QLatin1String("description")] = tag.replayLabSemanticTooltip;
        const QString title = tr("CBuffer parameter: %1 (EID %2)").arg(item->text(0)).arg(CBufferEvent());
        const QString json = ReplayLabAIResourceJSON(attachment);
        const QString text = ReplayLabAIResourceJSON(fieldDetails);
        QAction *addField = contextMenu.addAction(tr("Add parameter to AI conversation"));
        addField->setObjectName(QStringLiteral("addCBufferFieldToAI"));
        QObject::connect(addField, &QAction::triggered, [this, title, text, json]() {
          m_Ctx.AddTextToAIAssistant(title, text, title, json);
        });
      }
    }
  }

  // ReplayLab alias view: rename a reflected constant for display only. The alias lives in the
  // per-user store keyed by the constant block layout, never in the shader or the capture.
  if(IsCBufferView() && item && tag.valid && !tag.padding && !tag.path.isEmpty())
  {
    contextMenu.addSeparator();
    QAction *renameAlias = contextMenu.addAction(
        tag.alias.isEmpty() ? tr("&Rename (Create Alias)...") : tr("&Rename Alias..."));
    renameAlias->setIcon(Icons::page_white_edit());
    const QString aliasPath = tag.path;
    const QString aliasOriginal = QString(tag.name);
    const qint64 aliasOffset = tag.leaf ? (qint64)tag.byteOffset : -1;
    QObject::connect(renameAlias, &QAction::triggered, [this, aliasPath, aliasOriginal, aliasOffset]() {
      editCBufferAlias(aliasPath, aliasOriginal, aliasOffset, false);
    });
    if(!tag.alias.isEmpty())
    {
      QAction *removeAlias = contextMenu.addAction(tr("Remove Alias (Show %1)").arg(aliasOriginal));
      QObject::connect(removeAlias, &QAction::triggered, [this, aliasPath, aliasOriginal, aliasOffset]() {
        editCBufferAlias(aliasPath, aliasOriginal, aliasOffset, true);
      });
    }
  }

  if(IsCBufferView() && m_CBufferAliases.Loaded())
  {
    contextMenu.addSeparator();
    QAction *semanticView = contextMenu.addAction(tr("Show Semantic Fields"));
    semanticView->setCheckable(true);
    semanticView->setChecked(m_ShowCBufferSemanticFields);
    QObject::connect(semanticView, &QAction::toggled, [this](bool enabled) {
      UI_InvalidateCBufferLiveEdit();
      m_ShowCBufferSemanticFields = enabled;
      UI_RefreshCBufferAliasDisplay();
    });
    const ReplayLabCBufferAlias current = m_CBufferAliases.BlockAlias();
    QAction *renameBlockAlias = contextMenu.addAction(
        current.valid() ? tr("&Rename Constant Block Alias...")
                        : tr("&Rename Constant Block (Create Alias)..."));
    renameBlockAlias->setIcon(Icons::page_white_edit());
    QObject::connect(renameBlockAlias, &QAction::triggered,
                     [this]() { editCBufferBlockAlias(false); });
    if(current.valid())
    {
      QAction *removeBlockAlias = contextMenu.addAction(tr("Remove Constant Block Alias"));
      QObject::connect(removeBlockAlias, &QAction::triggered,
                       [this]() { editCBufferBlockAlias(true); });
    }
  }

  // Stock qrenderdoc previously exposed only the whole BufferViewer panel to extensions. ReplayLab
  // needs a variable-scoped hook so its material panel can start from a native selection instead
  // of accepting a caller-supplied byte offset. The bridge still re-reflects and validates every
  // value before it issues an editable leaf ID.
  if(IsCBufferView() && item && tag.valid && !tag.padding && CBufferEvent() == m_Ctx.CurEvent())
  {
    const ShaderReflection *reflection =
        CBufferReflection();
    const ConstantBlock *block = NULL;
    if(reflection && m_CBufferSlot.slot < reflection->constantBlocks.size())
      block = &reflection->constantBlocks[m_CBufferSlot.slot];

    uint64_t materialByteSize = m_ByteSize;
    bool materialRootCBV = false;
    if(block != NULL && m_Ctx.APIProps().pipelineType == GraphicsAPI::D3D12)
    {
      const D3D12Pipe::State *d3d12 = m_Ctx.CurD3D12PipelineState();
      if(d3d12 != NULL)
      {
        for(const D3D12Pipe::RootParam &param : d3d12->rootSignature.parameters)
        {
          if((param.visibility & MaskForStage(m_CBufferSlot.stage)) &&
             param.reg == block->fixedBindNumber &&
             param.space == block->fixedBindSetOrSpace &&
             param.descriptor.type == DescriptorType::ConstantBuffer &&
             param.descriptor.resource == m_BufferID &&
             param.descriptor.byteOffset == m_ByteOffset)
          {
            // Root CBVs have no descriptor size and the generic view reports the whole resource
            // remainder. Use exactly the reflected shader-visible block window instead.
            materialByteSize = ReplayLabMaterialBindingByteSize(
                GraphicsAPI::D3D12, m_ByteSize, block->byteSize, true);
            materialRootCBV = true;
            break;
          }
        }
      }
    }

    ReplayLabMaterialGateState gateState;
    gateState.api = m_Ctx.APIProps().pipelineType;
    gateState.remoteReplay = m_Ctx.APIProps().remoteReplay;
    const ActionDescription *currentAction = m_Ctx.CurAction();
    gateState.replayableAction =
        currentAction != NULL &&
        bool(currentAction->flags &
             (ActionFlags::Drawcall | ActionFlags::Dispatch | ActionFlags::MeshDispatch));
    gateState.viewReady = m_CurCBuffer.valid;
    gateState.reflectionAvailable = block != NULL;
    gateState.bytesBacked = m_CurCBuffer.bytesBacked;
    gateState.inlineData = block != NULL && block->inlineDataBytes;
    gateState.rootCBV = materialRootCBV;
    gateState.indirectAction =
        currentAction != NULL && bool(currentAction->flags & ActionFlags::Indirect);
    gateState.compileConstants = m_CurCBuffer.compileConstants;
    gateState.customFormat = !m_Format.isEmpty();
    gateState.materialLeaf = tag.replayLabMaterialLeaf;
    gateState.shaderBound = m_CurCBuffer.shader != ResourceId();
    gateState.bufferBound = m_BufferID != ResourceId();
    gateState.byteOffset = m_ByteOffset;
    gateState.byteSize = materialByteSize;
    if(block)
    {
      gateState.physicalSlot = block->fixedBindNumber;
      gateState.registerSpace = block->fixedBindSetOrSpace;
      gateState.bindArraySize = block->bindArraySize;
    }
    gateState.arrayIndex = m_CBufferSlot.arrayIdx;

    QString unavailable;
    switch(GetReplayLabMaterialGate(gateState))
    {
      case ReplayLabMaterialGate::Available: break;
      case ReplayLabMaterialGate::UnsupportedAPI:
        unavailable =
            tr("Shared material buffer editing is unavailable for the current capture API: %1")
                .arg(ToQStr(gateState.api));
        break;
      case ReplayLabMaterialGate::NotReplayableAction:
        unavailable = tr("Select an actual Draw, Dispatch, or MeshDispatch EID first");
        break;
      case ReplayLabMaterialGate::ViewNotReady:
        unavailable = tr("The CBuffer view has not finished loading its reflected binding");
        break;
      case ReplayLabMaterialGate::MissingReflection:
        unavailable = tr("The selected constant-block reflection is no longer available");
        break;
      case ReplayLabMaterialGate::NoRuntimeBacking:
        unavailable =
            tr("The selected constant block is not backed by editable runtime buffer bytes");
        break;
      case ReplayLabMaterialGate::RootConstants:
        unavailable = tr("Inline and push constants have no shared buffer; select a buffer-backed block");
        break;
      case ReplayLabMaterialGate::CompileConstants:
        unavailable = tr("Compile-time constants are not runtime CBuffer memory");
        break;
      case ReplayLabMaterialGate::CustomFormat:
        unavailable = tr("Clear the custom format to select a captured reflected CBuffer leaf");
        break;
      case ReplayLabMaterialGate::UnsupportedLeaf:
        unavailable =
            tr("Only reflected 32-bit float/int/uint scalar and vector leaves are editable");
        break;
      case ReplayLabMaterialGate::ShaderUnbound:
        unavailable = tr("No Shader is bound for this reflected constant block");
        break;
      case ReplayLabMaterialGate::BufferUnbound:
        unavailable = tr("The selected CBuffer binding is unbound at this event");
        break;
      case ReplayLabMaterialGate::InvalidBindingWindow:
        unavailable = tr("The bound CBuffer byte window is invalid or unsupported");
        break;
      case ReplayLabMaterialGate::UnsupportedBinding:
        unavailable = tr("The selected constant-buffer binding or array element is invalid");
        break;
    }

    if(!unavailable.isEmpty())
    {
      contextMenu.addSeparator();
      QAction *disabled = contextMenu.addAction(
          tr("Expose as Material Parameter (%1)").arg(unavailable));
      disabled->setEnabled(false);
    }
    else
    {
      ExtensionCallbackData callbackdata = {
          {"schema", lit("renderdoc-ultra-buffer-variable-selection/v1")},
          {"eventId", m_Ctx.CurEvent()},
          {"stageName", ToQStr(m_CBufferSlot.stage)},
          {"blockIndex", m_CBufferSlot.slot},
          {"arrayIndex", m_CBufferSlot.arrayIdx},
          {"physicalSlot", block->fixedBindNumber},
          {"registerSpace", block->fixedBindSetOrSpace},
          {"blockName", QString(block->name)},
          {"entryPoint", QString(m_CurCBuffer.entryPoint)},
          {"shaderResourceId", QVariant::fromValue(m_CurCBuffer.shader)},
          {"bufferResourceId", QVariant::fromValue(m_BufferID)},
          {"bufferByteOffset", qulonglong(m_ByteOffset)},
          {"bufferByteSize", qulonglong(materialByteSize)},
          {"fieldName", QString(tag.name)},
          {"fieldByteOffset", tag.byteOffset},
          {"fieldBaseType", ToQStr(tag.replayLabBaseType)},
          {"fieldComponents", uint32_t(tag.replayLabComponents)},
      };

      const int oldActionCount = contextMenu.actions().count();
      QAction *separator = contextMenu.addSeparator();
      m_Ctx.Extensions().MenuDisplaying(ContextMenu::BufferViewer_Variable, &contextMenu,
                                        callbackdata);
      if(contextMenu.actions().count() == oldActionCount + 1)
      {
        contextMenu.removeAction(separator);
        delete separator;
      }
    }
  }

  RDDialog::show(&contextMenu, ui->fixedVars->viewport()->mapToGlobal(pos));
}

void BufferViewer::stageRowMenu(MeshDataStage stage, QMenu *menu, const QPoint &pos)
{
  m_CurView = tableForStage(stage);
  m_CurFixed = false;

  updateExportActionNames();

  menu->clear();

  menu->setToolTipsVisible(true);

  QModelIndex idx = m_CurView->selectionModel()->currentIndex();

  QModelIndexList selectedRows = m_CurView->selectionModel()->selectedRows();
  bool multiSelected = selectedRows.count() > 1;

  if(multiSelected)
  {
    // build CSV now while selection is still valid
    QString csvData = m_CurView->buildSelectedCSV();

    m_CopyCSV->disconnect();
    QObject::connect(m_CopyCSV, &QAction::triggered, [csvData]() {
      QClipboard *clipboard = QApplication::clipboard();
      clipboard->setText(csvData);
    });

    menu->addAction(m_CopyCSV);
    menu->addSeparator();
    menu->addAction(m_ExportCSV);
    menu->addAction(m_ExportBytes);

    menu->popup(m_CurView->viewport()->mapToGlobal(pos));
    return;
  }

  const ActionDescription *action = m_Ctx.CurAction();

  if(action && (action->flags & ActionFlags::MeshDispatch))
  {
    if(stage == MeshDataStage::GSOut)
    {
      const BufferConfiguration &config = m_ModelOut2->getConfig();

      auto it = std::upper_bound(config.meshletVertexPrefixCounts.begin(),
                                 config.meshletVertexPrefixCounts.end(), (uint32_t)idx.row());

      if(it != config.meshletVertexPrefixCounts.begin())
        it--;

      size_t meshletIdx = it - config.meshletVertexPrefixCounts.begin();

      m_RemoveFilter->setEnabled(m_CurMeshFilter != MeshFilter::None);

      menu->addAction(m_RemoveFilter);
      menu->addAction(m_FilterMesh);
      menu->addAction(m_GotoTask);

      const ShaderReflection *shaderDetails =
          m_Ctx.CurPipelineState().GetShaderReflection(ShaderStage::Mesh);

      m_DebugMeshThread->setEnabled(false);

      if(!m_Ctx.APIProps().shaderDebugging)
      {
        m_DebugMeshThread->setToolTip(tr("This API does not support shader debugging"));
      }
      else if(!m_Ctx.CurAction() ||
              !(m_Ctx.CurAction()->flags & (ActionFlags::Drawcall | ActionFlags::MeshDispatch)))
      {
        m_DebugMeshThread->setToolTip(tr("No draw call selected"));
      }
      else if(!shaderDetails)
      {
        m_DebugMeshThread->setToolTip(tr("No mesh shader bound"));
      }
      else if(!shaderDetails->debugInfo.debuggable)
      {
        m_DebugMeshThread->setToolTip(
            tr("This shader doesn't support debugging: %1").arg(shaderDetails->debugInfo.debugStatus));
      }
      else
      {
        m_DebugMeshThread->setEnabled(true);
        m_DebugMeshThread->setToolTip(QString());
      }
      menu->addAction(m_DebugMeshThread);

      menu->addSeparator();

      m_GotoTask->setEnabled(m_Ctx.CurPipelineState().GetShaderReflection(ShaderStage::Task));
    }
  }

  if(m_MeshView && stage != MeshDataStage::GSOut)
  {
    const ShaderReflection *shaderDetails =
        m_Ctx.CurPipelineState().GetShaderReflection(ShaderStage::Vertex);

    m_DebugVert->setEnabled(false);

    if(!m_Ctx.APIProps().shaderDebugging)
    {
      m_DebugVert->setToolTip(tr("This API does not support shader debugging"));
    }
    else if(!m_Ctx.CurAction() ||
            !(m_Ctx.CurAction()->flags & (ActionFlags::Drawcall | ActionFlags::MeshDispatch)))
    {
      m_DebugVert->setToolTip(tr("No draw call selected"));
    }
    else if(!shaderDetails)
    {
      m_DebugVert->setToolTip(tr("No vertex shader bound"));
    }
    else if(!shaderDetails->debugInfo.debuggable)
    {
      m_DebugVert->setToolTip(
          tr("This shader doesn't support debugging: %1").arg(shaderDetails->debugInfo.debugStatus));
    }
    else
    {
      m_DebugVert->setEnabled(true);
      m_DebugVert->setToolTip(QString());
    }

    menu->addAction(m_DebugVert);
    menu->addSeparator();
  }

  menu->addAction(m_ExportCSV);
  menu->addAction(m_ExportBytes);

  menu->popup(m_CurView->viewport()->mapToGlobal(pos));

  ContextMenu contextMenu = ContextMenu::MeshPreview_VSInVertex;

  if(stage == MeshDataStage::VSOut)
    contextMenu = ContextMenu::MeshPreview_VSOutVertex;
  else if(stage == MeshDataStage::GSOut)
    contextMenu = ContextMenu::MeshPreview_GSOutVertex;
  else if(stage == MeshDataStage::TaskOut)
    contextMenu = ContextMenu::MeshPreview_TaskOutVertex;
  else if(stage == MeshDataStage::MeshOut)
    contextMenu = ContextMenu::MeshPreview_MeshOutVertex;

  ExtensionCallbackData callbackdata = {make_pyarg("stage", (uint32_t)stage)};

  if(idx.isValid())
  {
    uint32_t vertid =
        m_CurView->model()->data(m_CurView->model()->index(idx.row(), 0), Qt::DisplayRole).toUInt();
    uint32_t index =
        m_CurView->model()->data(m_CurView->model()->index(idx.row(), 1), Qt::DisplayRole).toUInt();

    callbackdata.push_back(make_pyarg("vertex", vertid));
    callbackdata.push_back(make_pyarg("index", index));
  }

  m_Ctx.Extensions().MenuDisplaying(contextMenu, menu, callbackdata);
}

BufferViewer::~BufferViewer()
{
  if(m_MeshView)
    qApp->removeEventFilter(this);
  UI_InvalidateCBufferLiveEdit();
  if(m_Output || m_UVThumbnailOutput)
  {
    m_Ctx.Replay().BlockInvoke([this](IReplayController *) {
      if(m_UVThumbnailOutput)
        m_UVThumbnailOutput->Shutdown();
      if(m_Output)
        m_Output->Shutdown();
    });
  }

  delete m_Arcball;
  delete m_Flycam;

  if(m_MeshView)
    m_Ctx.BuiltinWindowClosed(this);

  m_Ctx.RemoveCaptureViewer(this);
  delete ui;

  m_CBufferViews.removeOne(this);
}

void BufferViewer::OnCaptureLoaded()
{
  Reset();

  if(!m_MeshView)
    return;

  WindowingData winData = ui->render->GetWidgetWindowingData();

  m_Ctx.Replay().BlockInvoke([winData, this](IReplayController *r) {
    m_Output = r->CreateOutput(winData, ReplayOutputType::Mesh);

    ui->render->SetOutput(m_Output);

    RT_UpdateAndDisplay(r);
  });
}

void BufferViewer::OnCaptureClosed()
{
  if(m_UVData)
  {
    const QSignalBlocker channels(m_UVChannels);
    const QSignalBlocker textures(m_UVTextures);
    m_UVEvent = 0;
    m_UVDataSequence = -1;
    m_MeshEvidenceSelectionPending = false;
    m_MeshEvidenceRequest.clear();
    m_UVExplanationTexture = QImage();
    m_UVThumbnailOutput = NULL;
    m_UVManualChannel.clear();
    ++m_UVTextureSerial;
    ++m_UVGeometrySerial;
    m_UVTextureEvent = 0;
    m_MeshClipGeometry.reset();
    m_UVSelectedRows.clear();
    m_UVData->geometry.reset();
    m_UVData->texture = QImage();
    m_UVCanvas->geometryChanged();
    m_UVCanvas->textureChanged();
    m_UVChannels->clear();
    m_UVTextures->clear();
  }
  UI_InvalidateCBufferLiveEdit();
  ++m_Sequence;
  m_CBufferPinnedReflection.reset();
  m_CBufferPinnedEvent = m_CBufferDisplayedEvent = 0;
  setProperty("cbufferPinned", false);
  setProperty("cbufferPinnedEvent", 0);
  setProperty("cbufferDisplayedEvent", 0);
  if(m_CBufferPin)
  {
    const QSignalBlocker blocker(m_CBufferPin);
    m_CBufferPin->setChecked(false);
    m_CBufferPin->setEnabled(false);
  }
  if(m_CBufferAliasRefreshTimer)
    m_CBufferAliasRefreshTimer->stop();
  if(m_CBufferAliasWatcher)
  {
    if(!m_CBufferAliasWatcher->files().isEmpty())
      m_CBufferAliasWatcher->removePaths(m_CBufferAliasWatcher->files());
    if(!m_CBufferAliasWatcher->directories().isEmpty())
      m_CBufferAliasWatcher->removePaths(m_CBufferAliasWatcher->directories());
  }
  m_CBufferAliasFile.clear();
  m_CBufferAliasFileStamp.clear();
  m_CBufferAliasIdentity.clear();
  m_DeferredEventID = 0;
  m_HasDeferredEvent = false;
  Reset();

  if(!m_MeshView)
    ToolWindowManager::closeToolWindow(this);
}

void BufferViewer::showEvent(QShowEvent *event)
{
  QFrame::showEvent(event);

  if(!m_HasDeferredEvent || !m_Ctx.IsCaptureLoaded())
    return;

  const uint32_t eventId = m_DeferredEventID;
  m_HasDeferredEvent = false;
  QTimer::singleShot(0, this, [this, eventId]() {
    if(isVisible() && m_Ctx.IsCaptureLoaded())
      OnEventChanged(eventId);
  });
}

void BufferViewer::FillScrolls(PopulateBufferData *bufdata)
{
  bufdata->inHoriz = ui->inTable->horizontalScrollBar()->value();
  bufdata->out1Horiz = ui->out1Table->horizontalScrollBar()->value();
  bufdata->out2Horiz = ui->out2Table->horizontalScrollBar()->value();

  bufdata->inVert = ui->inTable->indexAt(QPoint(0, 0)).row();
  bufdata->out1Vert = ui->out1Table->indexAt(QPoint(0, 0)).row();
  bufdata->out2Vert = ui->out2Table->indexAt(QPoint(0, 0)).row();

  if(bufdata->meshDispatch)
  {
    bufdata->out1Horiz = ui->fixedVars->horizontalScrollBar()->value();
    bufdata->out1Vert = ui->fixedVars->indexOfTopLevelItem(ui->fixedVars->itemAt(QPoint(0, 0)));
  }
}

void BufferViewer::OnEventChanged(uint32_t eventId)
{
  if(m_UVData && m_UVEvent != eventId)
  {
    const QScopedValueRollback<bool> updating(m_UVSelectionUpdating, true);
    m_UVSelectedRows.clear();
    ++m_UVTextureSerial;
    ++m_UVGeometrySerial;
    m_UVTextureEvent = 0;
    m_UVData->geometry.reset();
    m_UVData->texture = QImage();
    m_UVCanvas->geometryChanged();
    m_UVCanvas->textureChanged();
    UpdateMeshUVPreview();
  }
  // A pinned viewer owns its binding snapshot. Event navigation must not replace it or
  // cancel an edit addressed to that snapshot; explicit refreshes use its anchor EID.
  if(m_CBufferPinnedReflection && eventId != m_CBufferPinnedEvent)
    return;
  if(m_CBufferLiveEdit && m_CBufferLiveEdit->eventId != eventId)
    UI_InvalidateCBufferLiveEdit();
  // Inactive dock tabs still receive capture notifications. Post-VS extraction and buffer/CBuffer
  // reads can materialise a cached virtual EID, so defer that work until the user actually exposes
  // this viewer. Visible mesh outputs are already treated as a hard replay requirement.
  if(!isVisible())
  {
    m_DeferredEventID = eventId;
    m_HasDeferredEvent = true;
    return;
  }

  m_HasDeferredEvent = false;
  PopulateBufferData *bufdata = new PopulateBufferData;
  bufdata->eventId = eventId;
  bufdata->bufferReadSize = m_ByteSize;
  if(m_CBufferPin && IsCBufferView())
    m_CBufferPin->setEnabled(bool(m_CBufferPinnedReflection));

  m_Sequence++;
  bufdata->sequence = m_Sequence;

  if(m_Scrolls)
  {
    bufdata->inHoriz = m_Scrolls->inHoriz;
    bufdata->out1Horiz = m_Scrolls->out1Horiz;
    bufdata->out2Horiz = m_Scrolls->out2Horiz;

    bufdata->inVert = m_Scrolls->inVert;
    bufdata->out1Vert = m_Scrolls->out1Vert;
    bufdata->out2Vert = m_Scrolls->out2Vert;

    delete m_Scrolls;
    m_Scrolls = NULL;
  }
  else
  {
    FillScrolls(bufdata);
  }

  // remove any pending scrolls, which have been applied. If nothing changes over the data
  // population the above scroll preserving will work.
  // however if m_Scroll is set while data is populationg, we'll apply it when it comes to the end
  m_Scroll[(int)MeshDataStage::VSIn] = QPoint(-1, -1);
  m_Scroll[(int)MeshDataStage::VSOut] = QPoint(-1, -1);
  m_Scroll[(int)MeshDataStage::GSOut] = QPoint(-1, -1);

  bufdata->highlightNames[0] = m_ModelIn->posName();
  bufdata->highlightNames[1] = m_ModelIn->secondaryName();
  bufdata->highlightNames[2] = m_ModelOut1->posName();
  bufdata->highlightNames[3] = m_ModelOut1->secondaryName();
  bufdata->highlightNames[4] = m_ModelOut2->posName();
  bufdata->highlightNames[5] = m_ModelOut2->secondaryName();

  const ActionDescription *action = m_Ctx.CurAction();

  bufdata->meshDispatch = action && (action->flags & ActionFlags::MeshDispatch);

  configureDrawRange();

  if(m_MeshView)
  {
    ClearModels();

    CalcColumnWidth();

    ClearModels();

    const PipeState &pipe = m_Ctx.CurPipelineState();

    if(pipe.IsRestartEnabled() && action && (action->flags & ActionFlags::Indexed))
    {
      bufdata->inConfig.primRestart = pipe.GetRestartIndex();

      if(pipe.GetIBuffer().byteStride == 1)
        bufdata->inConfig.primRestart &= 0xff;
      else if(pipe.GetIBuffer().byteStride == 2)
        bufdata->inConfig.primRestart &= 0xffff;

      bufdata->out1Config.primRestart = bufdata->inConfig.primRestart;
      // GS Out doesn't use primitive restart because it is post-expansion
    }

    ConfigureColumns(m_Ctx, bufdata);

    Viewport vp = m_Ctx.CurPipelineState().GetViewport(0);

    float vpWidth = qAbs(vp.width);
    float vpHeight = qAbs(vp.height);

    m_Config.fov = m_ProjGuess.fov;
    m_Config.aspect = (vpWidth > 0.0f && vpHeight > 0.0f) ? (vpWidth / vpHeight) : 1.0f;
    m_Config.highlightVert = 0;

    if(m_ProjGuess.aspect > 0.0)
      m_Config.aspect = m_ProjGuess.aspect;
  }
  else
  {
    // update with the current cbuffer for the current slot
    if(IsCBufferView())
    {
      UsedDescriptor cb = m_CBufferPinnedReflection
                              ? m_CBufferPinnedBinding
                              : m_Ctx.CurPipelineState().GetConstantBlock(
                                    m_CBufferSlot.stage, m_CBufferSlot.slot, m_CBufferSlot.arrayIdx);
      m_BufferID = cb.descriptor.resource;
      m_ByteOffset = cb.descriptor.byteOffset;
      m_ByteSize = cb.descriptor.byteSize;
      bufdata->bufferReadSize = m_ByteSize;

      const ShaderReflection *reflection =
          CBufferReflection();
      bufdata->cb.valid =
          (reflection != NULL && m_CBufferSlot.slot < reflection->constantBlocks.size());
      if(bufdata->cb.valid)
      {
        bufdata->cb.bytesBacked = reflection->constantBlocks[m_CBufferSlot.slot].bufferBacked ||
                                  reflection->constantBlocks[m_CBufferSlot.slot].inlineDataBytes;
        bufdata->cb.compileConstants =
            reflection->constantBlocks[m_CBufferSlot.slot].compileConstants;
        static const bool legacyRefresh =
            qEnvironmentVariableIntValue("RENDERDOC_CBUFFER_EDIT_LEGACY_REFRESH") == 1;
        if(m_Format.isEmpty() && !legacyRefresh)
          bufdata->bufferReadSize = CBufferReflectedReadSize(
              m_ByteSize, reflection->constantBlocks[m_CBufferSlot.slot].byteSize);
      }
      // Diagnostic properties are used by the native acceptance fixture. No data is cached
      // across an event, a binding change or a new shader declaration.
      setProperty("cbufferBindingBytes", qulonglong(cb.descriptor.byteSize));
      setProperty("cbufferReadBytes", qulonglong(bufdata->bufferReadSize));

      ui->setFormat->setEnabled(bufdata->cb.bytesBacked);
      if(ui->setFormat->isEnabled())
        ui->setFormat->setToolTip(tr("Specify a custom format for this constant buffer"));
      else
        ui->setFormat->setToolTip(tr("Cannot specify custom format without backing memory"));

      if(m_CBufferPinnedReflection)
        bufdata->cb = m_CBufferPinnedData;
      else
      {
        bufdata->cb.pipe = m_CBufferSlot.stage == ShaderStage::Compute
                               ? m_Ctx.CurPipelineState().GetComputePipelineObject()
                               : m_Ctx.CurPipelineState().GetGraphicsPipelineObject();
        bufdata->cb.shader = m_Ctx.CurPipelineState().GetShader(m_CBufferSlot.stage);
        bufdata->cb.entryPoint = m_Ctx.CurPipelineState().GetShaderEntryPoint(m_CBufferSlot.stage);
      }

      if(m_Format.isEmpty())
      {
        // stage, slot, and array index are all invariant when viewing a constant buffer
        // ee only need to use the actual bound shader as a key.
        RDTreeViewExpansionState &prevShaderExpansionState =
            ui->fixedVars->getInternalExpansion(qHash(ToQStr(m_CurCBuffer.shader)));

        ui->fixedVars->saveExpansion(prevShaderExpansionState, 0);
      }
    }

    ParsedFormat parsed = BufferFormatter::ParseFormatString(m_Format, m_ByteSize, IsCBufferView());

    bufdata->inConfig.fixedVars = parsed.fixed;
    bufdata->inConfig.packing = parsed.packing;

    if(parsed.repeating.type.baseType != VarType::Unknown)
    {
      bufdata->inConfig.repeatStride = parsed.repeating.type.arrayByteStride;
      bufdata->inConfig.repeatOffset = parsed.repeating.byteOffset;

      UnrollConstant(parsed.repeating, bufdata->inConfig.columns, bufdata->inConfig.props);
    }
    else
    {
      bufdata->inConfig.repeatStride = 1U;
      bufdata->inConfig.repeatOffset = parsed.fixed.type.arrayByteStride;
    }

    if((m_Format.isEmpty() || !bufdata->cb.bytesBacked) && IsCBufferView())
    {
      if(bufdata->cb.valid)
      {
        const ShaderReflection *reflection =
            CBufferReflection();

        bufdata->inConfig.fixedVars.type.members =
            reflection->constantBlocks[m_CBufferSlot.slot].variables;

        if(IsD3D(m_Ctx.APIProps().pipelineType))
          bufdata->inConfig.packing = PackingRules::D3DCB();
        else
          bufdata->inConfig.packing = BufferFormatter::EstimatePackingRules(
              reflection->resourceId, bufdata->inConfig.fixedVars.type.members);
      }
    }

    ClearModels();
  }

  updateLabelsAndLayout();

  bufdata->inConfig.curInstance = bufdata->out1Config.curInstance =
      bufdata->out2Config.curInstance = m_Config.curInstance;
  bufdata->inConfig.curView = bufdata->out1Config.curView = bufdata->out2Config.curView =
      m_Config.curView;

  m_ModelIn->beginReset();
  m_ModelOut1->beginReset();
  m_ModelOut2->beginReset();

  bufdata->inConfig.baseVertex = action ? action->baseVertex : 0;

  ui->formatSpecifier->setEnabled(!IsCBufferView() || bufdata->cb.bytesBacked);

  ui->instance->setEnabled(action && (action->flags & ActionFlags::Instanced));
  if(!ui->instance->isEnabled())
    ui->instance->setValue(0);

  if(action)
    ui->instance->setMaximum(qMax(0, int(action->numInstances) - 1));

  uint32_t numViews = m_Ctx.CurPipelineState().MultiviewBroadcastCount();

  if(action && numViews > 1)
  {
    ui->viewIndex->setEnabled(true);
    ui->viewIndex->setMaximum(qMax(0, int(numViews) - 1));
  }
  else
  {
    ui->viewIndex->setEnabled(false);
    ui->viewIndex->setValue(0);
  }

  QPointer<BufferViewer> me(this);
  const uint32_t pinnedEvent = m_CBufferPinnedReflection ? m_CBufferPinnedEvent : 0;
  const QString pinnedHash =
      pinnedEvent ? ReplayLabCurrentShaderRawSha256(CBufferReflection()) : QString();
  const ResourceId bufferId = m_BufferID;
  const uint64_t byteOffset = m_ByteOffset;
  const auto cbufferSlot = m_CBufferSlot;
  const bool evaluateCBuffer = IsCBufferView() && (m_Format.isEmpty() || !bufdata->cb.bytesBacked);
  uint32_t cbufferBind = ~0U, cbufferSpace = ~0U;
  if(evaluateCBuffer && bufdata->cb.valid)
  {
    const ConstantBlock &block = CBufferReflection()->constantBlocks[cbufferSlot.slot];
    cbufferBind = block.fixedBindNumber;
    cbufferSpace = block.fixedBindSetOrSpace;
  }

  m_Ctx.Replay().AsyncInvoke([this, me, bufdata, pinnedEvent, pinnedHash, bufferId, byteOffset,
                              cbufferSlot, cbufferBind, cbufferSpace, evaluateCBuffer](IReplayController *r) {
    if(!me)
    {
      delete bufdata;
      return;
    }

    CBufferReplayEventScope replayEvent(r, pinnedEvent);
    if(pinnedEvent)
    {
      const PipeState &pipe = r->GetPipelineState();
      const UsedDescriptor binding = pipe.GetConstantBlock(
          cbufferSlot.stage, cbufferSlot.slot, cbufferSlot.arrayIdx);
      if(r->GetCurrentEventID() != pinnedEvent || binding.descriptor.resource != bufferId ||
         binding.descriptor.byteOffset != byteOffset ||
         binding.descriptor.byteSize < bufdata->bufferReadSize ||
         ReplayLabCurrentShaderRawSha256(pipe.GetShaderReflection(cbufferSlot.stage)) != pinnedHash)
      {
        bufdata->cb.valid = false;
        bufdata->inConfig.fixedVars.type.members.clear();
        bufdata->cbufferError = QCoreApplication::translate(
            "BufferViewer", "The CBuffer binding changed; select the value again.");
      }
    }

    BufferData *buf = NULL;

    if(m_MeshView)
    {
      if(bufdata->meshDispatch)
      {
        bufdata->postOut1 = r->GetPostVSData(0, bufdata->inConfig.curView, MeshDataStage::TaskOut);
        bufdata->postOut2 = r->GetPostVSData(0, bufdata->inConfig.curView, MeshDataStage::MeshOut);

        const uint32_t vertsPerPrim = RENDERDOC_NumVerticesPerPrimitive(bufdata->postOut2.topology);

        // apply mesh/task filtering to mesh data here, which will also propagate to preview
        if(m_FilteredMeshGroup != ~0U)
        {
          bufdata->out1Config.taskOrMeshletOffset = m_FilteredTaskGroup;

          // find this meshlet's offset in the index buffer and filter to only it
          uint32_t indexCount = 0, vertexCount = 0;
          for(uint32_t i = 0; i <= m_FilteredMeshGroup && i < bufdata->postOut2.meshletSizes.size();
              i++)
          {
            MeshletSize meshletSize = bufdata->postOut2.meshletSizes[i];
            uint32_t numIndices = meshletSize.numIndices;
            if(i == m_FilteredMeshGroup)
            {
              bufdata->postOut2.meshletIndexOffset = vertexCount;
              bufdata->postOut2.meshletOffset = m_FilteredMeshGroup;
              bufdata->out2Config.taskOrMeshletOffset = m_FilteredMeshGroup;
              bufdata->postOut2.numIndices = numIndices;
              bufdata->postOut2.meshletSizes = {meshletSize};
              bufdata->postOut2.indexByteOffset += indexCount * bufdata->postOut2.indexByteStride;

              bufdata->postOut2.perPrimitiveOffset +=
                  (indexCount / vertsPerPrim) * bufdata->postOut2.perPrimitiveStride;
            }
            indexCount += numIndices;
            vertexCount += meshletSize.numVertices;
          }
        }
        else if(m_FilteredTaskGroup != ~0U)
        {
          bufdata->out1Config.taskOrMeshletOffset = m_FilteredTaskGroup;

          // find the relevant task and which mesh indices it corresponds to
          uint32_t meshletCounter = 0;
          for(uint32_t taskIndex = 0;
              taskIndex <= m_FilteredTaskGroup && taskIndex < bufdata->postOut1.taskSizes.size();
              taskIndex++)
          {
            uint32_t numMeshesInTask = bufdata->postOut1.taskSizes[taskIndex].x *
                                       bufdata->postOut1.taskSizes[taskIndex].y *
                                       bufdata->postOut1.taskSizes[taskIndex].z;

            // once we've found the desired task, filter our view to only its meshes
            if(taskIndex == m_FilteredTaskGroup)
            {
              bufdata->postOut2.numIndices = 0;

              rdcarray<MeshletSize> meshletSizes;
              meshletSizes.reserve(numMeshesInTask);

              uint32_t indexCount = 0, vertexCount = 0;
              for(uint32_t i = 0;
                  i < meshletCounter + numMeshesInTask && i < bufdata->postOut2.meshletSizes.size();
                  i++)
              {
                uint32_t indicesInMeshlet = bufdata->postOut2.meshletSizes[i].numIndices;

                if(i >= meshletCounter)
                {
                  bufdata->postOut2.numIndices += indicesInMeshlet;
                  meshletSizes.push_back(bufdata->postOut2.meshletSizes[i]);
                }

                if(i == meshletCounter)
                {
                  bufdata->postOut2.meshletIndexOffset = vertexCount;
                  bufdata->postOut2.meshletOffset = meshletCounter;
                  bufdata->out2Config.taskOrMeshletOffset = meshletCounter;
                  bufdata->postOut2.indexByteOffset += indexCount * bufdata->postOut2.indexByteStride;

                  bufdata->postOut2.perPrimitiveOffset +=
                      (indexCount / vertsPerPrim) * bufdata->postOut2.perPrimitiveStride;
                }
                indexCount += indicesInMeshlet;
                vertexCount += bufdata->postOut2.meshletSizes[i].numVertices;
              }

              bufdata->postOut2.meshletSizes = meshletSizes;

              break;
            }

            meshletCounter += numMeshesInTask;
          }
        }

        RT_FetchMeshPipeData(r, m_Ctx, bufdata);
      }
      else
      {
        bufdata->postOut1 = r->GetPostVSData(bufdata->inConfig.curInstance,
                                             bufdata->inConfig.curView, MeshDataStage::VSOut);
        bufdata->postOut2 = r->GetPostVSData(bufdata->inConfig.curInstance,
                                             bufdata->inConfig.curView, MeshDataStage::GSOut);

        RT_FetchVertexPipeData(r, m_Ctx, bufdata);
      }

      const PipeState &meshPipe = r->GetPipelineState();
      if(meshPipe.GetShader(ShaderStage::Mesh) != ResourceId())
        ApplyMeshChannelAliases(r, meshPipe, ShaderStage::Mesh, false, bufdata->out2Config);
      else
      {
        ApplyMeshChannelAliases(r, meshPipe, ShaderStage::Vertex, true, bufdata->inConfig);
        ApplyMeshChannelAliases(r, meshPipe, ShaderStage::Vertex, false, bufdata->out1Config);
        const ShaderStage lastStage = meshPipe.GetShader(ShaderStage::Geometry) != ResourceId()
                                          ? ShaderStage::Geometry
                                          : ShaderStage::Domain;
        ApplyMeshChannelAliases(r, meshPipe, lastStage, false, bufdata->out2Config);
      }

      if(!me)
      {
        delete bufdata;
        return;
      }
    }
    else
    {
      buf = new BufferData;

      // calculate tight stride
      buf->stride = std::max(1U, bufdata->inConfig.repeatStride);

      // we want to fetch the data for fixed and repeated sections (either of which might be 0)
      // but calculate the number of rows etc for the repeated sections based on just the data
      // available for it
      const uint64_t fixedLength = bufdata->inConfig.repeatOffset;

      // The reflected CBuffer view only reads its declaration; retain m_ByteSize as the
      // original binding identity for editing, custom formats and explicit raw export.
      uint64_t repeatedRangeStart = byteOffset + fixedLength;
      uint64_t repeatedRangeEnd = byteOffset + bufdata->bufferReadSize;

      // if the byte size is unbounded, the end is unbounded - fix the potential overflow from
      // adding the offset
      if(bufdata->bufferReadSize == UINT64_MAX)
        repeatedRangeEnd = UINT64_MAX;

      // get the underlying buffer length
      uint64_t bufferLength = 0;

      if(m_IsBuffer && bufferId != ResourceId())
      {
        const BufferDescription *desc = m_Ctx.GetBuffer(bufferId);
        if(desc)
          bufferLength = desc->length;
      }

      // clamp the range to the buffer length, which may end up with it being empty
      repeatedRangeEnd = qMin(repeatedRangeEnd, bufferLength);
      repeatedRangeStart = qMin(repeatedRangeStart, bufferLength);

      // store the number of rows unclamped without the paging window
      bufdata->inConfig.unclampedNumRows =
          uint32_t((repeatedRangeEnd - repeatedRangeStart + buf->stride - 1) / buf->stride);

      // advance the range by the paging offset
      repeatedRangeStart = qMin(repeatedRangeEnd, repeatedRangeStart + m_PagingByteOffset);

      // calculate the length clamped to the MaxVisibleRows
      const uint64_t clampedRepeatedLength =
          qMin(repeatedRangeEnd - repeatedRangeStart, uint64_t(buf->stride * (MaxVisibleRows + 2)));

      if(m_IsBuffer)
      {
        if(bufferId == ResourceId() || !bufdata->cbufferError.isEmpty())
        {
          buf->storage.clear();
        }
        else if(repeatedRangeStart > fixedLength)
        {
          // if the repeated range subsection we're fetching is paged further in, we still need to
          // fetch the fixed data from the 'start'
          if(fixedLength > 0)
            buf->storage = r->GetBufferData(bufferId, byteOffset, fixedLength);
          // then append the data from where we're paged to
          buf->storage.append(r->GetBufferData(bufferId, repeatedRangeStart, clampedRepeatedLength));
        }
        else
        {
          // otherwise we can fetch it all at once
          buf->storage =
              r->GetBufferData(bufferId, byteOffset, fixedLength + clampedRepeatedLength);
        }
      }
      else
      {
        buf->storage = r->GetTextureData(bufferId, m_TexSub);

        // recalculate total size for this subresource based on the data returned
        if(!buf->storage.empty())
          m_ObjectByteSize = buf->storage.size();
      }

      uint32_t repeatedDataAvailable = uint32_t(buf->size());
      if(repeatedDataAvailable > fixedLength)
        repeatedDataAvailable -= fixedLength;

      bufdata->inConfig.pagingOffset = uint32_t(m_PagingByteOffset / buf->stride);
      bufdata->inConfig.numRows = uint32_t((repeatedDataAvailable + buf->stride - 1) / buf->stride);

      // ownership passes to model
      bufdata->inConfig.buffers.push_back(buf);

      if(!me)
      {
        delete buf;
        delete bufdata;
        return;
      }
    }

    // for cbuffers, if the format is empty or if we're not buffer-backed and don't have inline
    // data, we evaluate variables here and don't use the format override with a fetched buffer
    if(evaluateCBuffer)
    {
      // only fetch the cbuffer constants if this binding is currently valid
      if(bufdata->cb.valid)
      {
        bufdata->inConfig.evalVars = r->GetCBufferVariableContents(
            bufdata->cb.pipe, bufdata->cb.shader, cbufferSlot.stage, bufdata->cb.entryPoint,
            cbufferSlot.slot, bufferId, byteOffset, bufdata->bufferReadSize);
        const ResourceId target = r->GetTargetShaderReplacement(bufdata->cb.shader);
        if(target != ResourceId() && target != bufdata->cb.shader)
        {
          ShaderEntryPoint entry;
          entry.name = bufdata->cb.entryPoint;
          entry.stage = cbufferSlot.stage;
          const ShaderReflection *display = r->GetShader(ResourceId(), target, entry);
          const ConstantBlock *match = NULL;
          bool unique = true;
          if(display)
            for(const ConstantBlock &block : display->constantBlocks)
              if(block.fixedBindNumber == cbufferBind && block.fixedBindSetOrSpace == cbufferSpace)
              {
                unique &= match == NULL;
                match = &block;
              }
          if(unique && match)
            bufdata->inConfig.cbufferDisplayVars = match->variables;
        }
      }
    }

    // Restore replay state before publishing UI data. No event-selection notification is
    // emitted for the private read, so other views continue to follow the user's event.
    replayEvent.Restore();
    GUIInvoke::call(this, [this, bufdata]() {
      if(bufdata->sequence != m_Sequence)
      {
        delete bufdata;
        return;
      }

      if(!bufdata->out1Config.statusString.isEmpty())
      {
        bufdata->out1Config.columns.clear();
        bufdata->out1Config.props.clear();
        ConfigureStatusColumn(bufdata->out1Config.columns, bufdata->out1Config.props);
      }

      if(!bufdata->out2Config.statusString.isEmpty())
      {
        bufdata->out2Config.columns.clear();
        bufdata->out2Config.props.clear();
        ConfigureStatusColumn(bufdata->out2Config.columns, bufdata->out2Config.props);
      }

      m_ModelIn->endReset(bufdata->inConfig);
      m_ModelOut1->endReset(bufdata->out1Config);
      m_ModelOut2->endReset(bufdata->out2Config);

      m_Out1Data = bufdata->postOut1;
      m_Out2Data = bufdata->postOut2;

      m_CurCBuffer = bufdata->cb;
      if(IsCBufferView())
      {
        m_CBufferDisplayedEvent = bufdata->eventId;
        setProperty("cbufferDisplayedEvent", m_CBufferDisplayedEvent);
        m_CBufferPin->setEnabled(bool(m_CBufferPinnedReflection) ||
                                (m_CurCBuffer.valid && m_CBufferDisplayedEvent == m_Ctx.CurEvent()));
      }

      // if we didn't have a position column selected before, or the name has changed, re-guess
      if(m_ModelIn->posColumn() == -1 ||
         bufdata->highlightNames[0] != bufdata->inConfig.columnName(m_ModelIn->posColumn()))
        m_ModelIn->setPosColumn(-1);
      // similarly for secondary columns
      if(m_ModelIn->secondaryColumn() == -1 ||
         bufdata->highlightNames[1] != bufdata->inConfig.columnName(m_ModelIn->secondaryColumn()))
        m_ModelIn->setSecondaryColumn(-1, m_Config.visualisationMode == Visualisation::Secondary,
                                      false);

      // and as above for VS Out / GS Out
      if(m_ModelOut1->posColumn() == -1 ||
         bufdata->highlightNames[2] != bufdata->out1Config.columnName(m_ModelOut1->posColumn()))
        m_ModelOut1->setPosColumn(-1);
      if(m_ModelOut1->secondaryColumn() == -1 ||
         bufdata->highlightNames[3] != bufdata->out1Config.columnName(m_ModelOut1->secondaryColumn()))
        m_ModelOut1->setSecondaryColumn(-1, m_Config.visualisationMode == Visualisation::Secondary,
                                        false);

      if(m_ModelOut2->posColumn() == -1 ||
         bufdata->highlightNames[4] != bufdata->out2Config.columnName(m_ModelOut2->posColumn()))
        m_ModelOut2->setPosColumn(-1);
      if(m_ModelOut2->secondaryColumn() == -1 ||
         bufdata->highlightNames[5] != bufdata->out2Config.columnName(m_ModelOut2->secondaryColumn()))
        m_ModelOut2->setSecondaryColumn(-1, m_Config.visualisationMode == Visualisation::Secondary,
                                        false);

      UpdateStageDataControls();

      populateBBox(bufdata);

      UI_ConfigureFormats();
      UpdateCurrentMeshConfig();
      m_UVEvent = bufdata->eventId;
      m_UVDataSequence = bufdata->sequence;
      UpdateMeshUV();

      ApplyRowAndColumnDims(
          m_ModelIn->columnCount(), ui->inTable,
          bufdata->inConfig.statusString.isEmpty() ? m_DataColWidth : m_ErrorColWidth);
      ApplyRowAndColumnDims(
          m_ModelOut1->columnCount(), ui->out1Table,
          bufdata->out1Config.statusString.isEmpty() ? m_DataColWidth : m_ErrorColWidth);
      ApplyRowAndColumnDims(
          m_ModelOut2->columnCount(), ui->out2Table,
          bufdata->out2Config.statusString.isEmpty() ? m_DataColWidth : m_ErrorColWidth);

      uint32_t numRows = qMax(qMax(bufdata->inConfig.numRows, bufdata->out1Config.numRows),
                              bufdata->out2Config.numRows);

      if(!m_MeshView)
        numRows = qMax(numRows, bufdata->inConfig.unclampedNumRows);

      ui->rowOffset->setMaximum((int)qMax(1U, numRows) - 1);

      ScrollToRow(ui->inTable, qMin(int(bufdata->inConfig.numRows) - 1, bufdata->inVert));
      ScrollToRow(ui->out1Table, qMin(int(bufdata->out1Config.numRows) - 1, bufdata->out1Vert));
      ScrollToRow(ui->out2Table, qMin(int(bufdata->out2Config.numRows) - 1, bufdata->out2Vert));

      ui->inTable->horizontalScrollBar()->setValue(bufdata->inHoriz);
      ui->out1Table->horizontalScrollBar()->setValue(bufdata->out1Horiz);
      ui->out2Table->horizontalScrollBar()->setValue(bufdata->out2Horiz);

      for(MeshDataStage stage : {MeshDataStage::VSIn, MeshDataStage::VSOut, MeshDataStage::GSOut})
      {
        int i = (int)stage;

        if(m_Scroll[i].y() >= 0)
          ScrollToRow(tableForStage(stage), m_Scroll[i].y());
        if(m_Scroll[i].x() >= 0)
          ScrollToColumn(tableForStage(stage), m_Scroll[i].x());

        m_Scroll[i] = QPoint(-1, -1);
      }

      if(m_MeshView)
      {
        RDTreeViewExpansionState state;
        ui->fixedVars->saveExpansion(state, 0);

        ui->fixedVars->beginUpdate();

        ui->fixedVars->clear();

        if(bufdata->meshDispatch && !bufdata->out1Config.statusString.isEmpty())
        {
          RDTreeWidgetItem *n =
              new RDTreeWidgetItem({lit("-, -, -"), bufdata->out1Config.statusString, QString()});

          ui->fixedVars->addTopLevelItem(n);
        }
        else if(bufdata->meshDispatch && !bufdata->out1Config.taskSizes.empty())
        {
          const ActionDescription *action = m_Ctx.CurAction();

          uint32_t i = 0;
          for(uint32_t x = 0; x < bufdata->out1Config.dispatchSize[0]; x++)
          {
            for(uint32_t y = 0; y < bufdata->out1Config.dispatchSize[1]; y++)
            {
              for(uint32_t z = 0; z < bufdata->out1Config.dispatchSize[2]; z++)
              {
                TaskGroupSize size = bufdata->out1Config.taskSizes[i];

                RDTreeWidgetItem *n = NULL;

                if(m_CurMeshFilter == MeshFilter::None || m_FilteredTaskGroup == i)
                {
                  n = new RDTreeWidgetItem(
                      {QFormatStr("%1, %2, %3").arg(x).arg(y).arg(z),
                       QFormatStr("Dispatched [%1, %2, %3]").arg(size.x).arg(size.y).arg(size.z),
                       lit("Task Group")});

                  ui->fixedVars->addTopLevelItem(n);
                }

                if(n && !bufdata->out1Config.columns.empty())
                {
                  UI_AddTaskPayloads(n, i * bufdata->out1Config.buffers[0]->stride,
                                     bufdata->out1Config.columns, bufdata->out1Config.buffers[0]);
                }

                i++;
              }
            }
          }
        }

        ui->fixedVars->endUpdate();

        ResourceId shader = m_Ctx.CurPipelineState().GetShader(ShaderStage::Task);

        // if we have saved expansion state for the new shader, apply it, otherwise apply the
        // previous one to get any overlap (e.g. two different shaders with very similar or
        // identical constants)
        if(ui->fixedVars->hasInternalExpansion(qHash(ToQStr(shader))))
          ui->fixedVars->applyExpansion(ui->fixedVars->getInternalExpansion(qHash(ToQStr(shader))),
                                        0);
        else
          ui->fixedVars->applyExpansion(state, 0);

        if(bufdata->out1Vert >= 0 && bufdata->out1Vert < ui->fixedVars->topLevelItemCount())
        {
          ScrollToRow(bufdata->out1Vert, MeshDataStage::TaskOut);
          ui->fixedVars->horizontalScrollBar()->setValue(bufdata->out1Horiz);
        }
      }

      if(!m_MeshView)
      {
        m_RepeatedOffset->setText(
            tr("Starting at: %1 bytes")
                .arg(Formatter::HumanFormat(m_ByteOffset + bufdata->inConfig.repeatOffset,
                                            Formatter::OffsetSize)));

        {
          rdcarray<ShaderVariable> vars;

          if(m_BufferID == ResourceId() || m_Format.isEmpty() || !bufdata->cbufferError.isEmpty())
          {
            vars = bufdata->inConfig.evalVars;
          }
          else
          {
            ShaderVariable var = InterpretShaderVar(bufdata->inConfig.fixedVars,
                                                    bufdata->inConfig.buffers[0]->storage.begin(),
                                                    bufdata->inConfig.buffers[0]->storage.end());

            vars.swap(var.members);
          }

          bool wasEmpty = ui->fixedVars->topLevelItemCount() == 0;

          RDTreeViewExpansionState state;
          ui->fixedVars->saveExpansion(state, 0);

          ui->fixedVars->beginUpdate();

          ui->fixedVars->clear();

          if(!vars.isEmpty())
          {
            UI_LoadCBufferAliases();
            // Build and reorganize detached rows. Per-row removals in the live
            // model otherwise emit thousands of Qt notifications while loading.
            RDTreeWidgetItem stagedRows;
            UI_AddFixedVariables(&stagedRows, 0, bufdata->inConfig.fixedVars.type.members, vars);
            if(IsCBufferView() && m_ShowCBufferSemanticFields && m_Format.isEmpty() &&
               !bufdata->cb.compileConstants)
              UI_ApplyCBufferSemanticFields(&stagedRows, bufdata->inConfig.cbufferDisplayVars);

            if(IsCBufferView() && !bufdata->cb.bytesBacked)
              UI_RemoveOffsets(&stagedRows);
            AppendCBufferChildren(ui->fixedVars->invisibleRootItem(), &stagedRows);
          }

          ui->fixedVars->endUpdate();

          if(wasEmpty)
          {
            // Expand before resizing so that collapsed data will already be visible when expanded
            ui->fixedVars->expandAll();
            for(int i = 0; i < ui->fixedVars->header()->count(); i++)
              ui->fixedVars->resizeColumnToContents(i);
            ui->fixedVars->collapseAll();
          }

          // if we have saved expansion state for the new shader, apply it, otherwise apply the
          // previous one to get any overlap (e.g. two different shaders with very similar or
          // identical constants)
          if(ui->fixedVars->hasInternalExpansion(qHash(ToQStr(m_CurCBuffer.shader))))
            ui->fixedVars->applyExpansion(
                ui->fixedVars->getInternalExpansion(qHash(ToQStr(m_CurCBuffer.shader))), 0);
          else
            ui->fixedVars->applyExpansion(state, 0);
          UI_RestoreCBufferLiveSelection();
          if(m_CBufferLiveEdit && !bufdata->cbufferError.isEmpty())
          {
            UI_InvalidateCBufferLiveEdit();
            m_CBufferLiveEdit->panel->setProperty("error", bufdata->cbufferError);
            m_CBufferLiveEdit->status->setText(bufdata->cbufferError);
          }
          UI_UpdateCBufferSearch();
          UI_ApplyCBufferNavigation();
        }

        on_rowOffset_valueChanged(ui->rowOffset->value());

        const bool prev = (bufdata->inConfig.pagingOffset > 0);
        const bool next = (bufdata->inConfig.numRows >= MaxVisibleRows);

        if(prev && next)
        {
          ui->inTable->setIndexWidget(m_ModelIn->index(0, 0), MakePreviousPageButton());
          ui->inTable->setIndexWidget(m_ModelIn->index(0, 1), MakeNextPageButton());

          ui->inTable->setIndexWidget(m_ModelIn->index(MaxVisibleRows + 1, 0),
                                      MakePreviousPageButton());
          ui->inTable->setIndexWidget(m_ModelIn->index(MaxVisibleRows + 1, 1), MakeNextPageButton());
        }
        else if(prev)
        {
          ui->inTable->setIndexWidget(m_ModelIn->index(0, 0), MakePreviousPageButton());
        }
        else if(next)
        {
          ui->inTable->setIndexWidget(m_ModelIn->index(MaxVisibleRows, 1), MakeNextPageButton());
        }
      }

      // we're done with it, the buffer configurations are individually copied/refcounted
      delete bufdata;

      INVOKE_MEMFN(RT_UpdateAndDisplay);
    });
  });
}

void BufferViewer::populateBBox(PopulateBufferData *bufdata)
{
  const ActionDescription *action = m_Ctx.CurAction();

  if(action && m_MeshView)
  {
    uint32_t eventId = action->eventId;
    bool calcNeeded = false;

    {
      QMutexLocker autolock(&m_BBoxLock);
      calcNeeded = !m_BBoxes.contains(eventId);
    }

    if(!calcNeeded)
    {
      if(!m_MeshCameraInitialised)
        UI_ResetArcball();
      return;
    }

    {
      QMutexLocker autolock(&m_BBoxLock);
      m_BBoxes.insert(eventId, BBoxData());
    }

    CalcBoundingBoxData *bbox = new CalcBoundingBoxData;

    bbox->eventId = eventId;

    bbox->input[0] = bufdata->inConfig;
    bbox->input[1] = bufdata->out1Config;
    bbox->input[2] = bufdata->out2Config;

    QPointer<BufferViewer> me(this);

    // fire up a thread to calculate the bounding box
    LambdaThread *thread = new LambdaThread([this, me, bbox] {
      if(!me)
      {
        delete bbox;
        return;
      }

      calcBoundingData(*bbox);

      if(!me)
      {
        delete bbox;
        return;
      }

      GUIInvoke::call(this, [this, bbox]() { UI_UpdateBoundingBox(*bbox); });
    });
    thread->setName(lit("BBox calc"));
    thread->selfDelete(true);
    thread->start();

    // give the thread a few ms to finish, so we don't get a tiny flicker on small/fast meshes
    thread->wait(10);
  }
}

QVariant BufferViewer::persistData()
{
  QVariantMap state;
  state = ui->dockarea->saveState();
  state[lit("axisMappingIndex")] = ui->axisMappingCombo->currentIndex();
  QVariantList xAxisMapping = {QVariant(m_Config.axisMapping.xAxis.x),
                               QVariant(m_Config.axisMapping.xAxis.y),
                               QVariant(m_Config.axisMapping.xAxis.z)};
  state[lit("xAxisMapping")] = xAxisMapping;
  QVariantList yAxisMapping = {QVariant(m_Config.axisMapping.yAxis.x),
                               QVariant(m_Config.axisMapping.yAxis.y),
                               QVariant(m_Config.axisMapping.yAxis.z)};
  state[lit("yAxisMapping")] = yAxisMapping;
  QVariantList zAxisMapping = {QVariant(m_Config.axisMapping.zAxis.x),
                               QVariant(m_Config.axisMapping.zAxis.y),
                               QVariant(m_Config.axisMapping.zAxis.z)};
  state[lit("zAxisMapping")] = zAxisMapping;

  return state;
}

void BufferViewer::setPersistData(const QVariant &persistData)
{
  QVariantMap state = persistData.toMap();

  ui->dockarea->restoreState(state);
  previousAxisMappingIndex = state[lit("axisMappingIndex")].toInt();
  ui->axisMappingCombo->setCurrentIndex(previousAxisMappingIndex);
  if(!state[lit("xAxisMapping")].toList().isEmpty())
  {
    m_Config.axisMapping.xAxis.x = state[lit("xAxisMapping")].toList()[0].toInt();
    m_Config.axisMapping.xAxis.y = state[lit("xAxisMapping")].toList()[1].toInt();
    m_Config.axisMapping.xAxis.z = state[lit("xAxisMapping")].toList()[2].toInt();
    m_Config.axisMapping.yAxis.x = state[lit("yAxisMapping")].toList()[0].toInt();
    m_Config.axisMapping.yAxis.y = state[lit("yAxisMapping")].toList()[1].toInt();
    m_Config.axisMapping.yAxis.z = state[lit("yAxisMapping")].toList()[2].toInt();
    m_Config.axisMapping.zAxis.x = state[lit("zAxisMapping")].toList()[0].toInt();
    m_Config.axisMapping.zAxis.y = state[lit("zAxisMapping")].toList()[1].toInt();
    m_Config.axisMapping.zAxis.z = state[lit("zAxisMapping")].toList()[2].toInt();
  }
}

void BufferViewer::UI_FixedAddMatrixRows(RDTreeWidgetItem *n, const ShaderConstant &c,
                                         const ShaderVariable &v)
{
  const bool showPadding = ui->showPadding->isChecked() && m_CurCBuffer.bytesBacked;

  if(v.rows > 1)
  {
    uint32_t vecSize = VarTypeByteSize(v.type) * v.columns;

    FixedVarTag tag = n->tag().value<FixedVarTag>();
    tag.matrix = true;
    tag.rowmajor = v.RowMajor();
    n->setTag(QVariant::fromValue(tag));

    const QString displayName = tag.alias.isEmpty() ? QString(v.name) : tag.alias;

    if(v.ColMajor())
      vecSize = VarTypeByteSize(v.type) * v.rows;

    for(uint32_t r = 0; r < v.rows; r++)
    {
      const FixedVarTag rowTag = ReplayLabMatrixRowTag(c, v, tag, r);
      QStringList offsets;
      for(quint64 offset : rowTag.replayLabEditOffsets)
        offsets.push_back(QString::number(offset));
      RDTreeWidgetItem *row = new RDTreeWidgetItem({QFormatStr("%1.row%2").arg(displayName).arg(r),
                                                    RowString(v, r), offsets.join(QStringLiteral(", ")),
                                                    RowTypeString(v)});
      row->setTag(QVariant::fromValue(rowTag));
      n->addChild(row);

      if(showPadding && v.RowMajor() && c.type.matrixByteStride > vecSize)
      {
        uint32_t size = c.type.matrixByteStride - vecSize;

        RDTreeWidgetItem *pad = new RDTreeWidgetItem({
            tr(""),
            QFormatStr("%1 bytes").arg(Formatter::HumanFormat(size, Formatter::OffsetSize)),
            QString(),
            tr("Padding"),
        });

        pad->setItalic(true);
        pad->setTag(QVariant::fromValue(FixedVarTag(size)));

        n->addChild(pad);
      }
    }

    if(showPadding && v.ColMajor() && c.type.matrixByteStride > vecSize)
    {
      uint32_t size = c.type.matrixByteStride - vecSize;

      RDTreeWidgetItem *pad = new RDTreeWidgetItem({
          tr(""),
          QFormatStr("%1 bytes each column").arg(Formatter::HumanFormat(size, Formatter::OffsetSize)),
          QString(),
          tr("Padding"),
      });

      pad->setItalic(true);
      pad->setTag(QVariant::fromValue(FixedVarTag(size)));

      n->addChild(pad);
    }
  }
}

static void TaskAddMatrixRows(RDTreeWidgetItem *n, const ShaderConstant &c, const ShaderVariable &v)
{
  if(v.rows > 1)
  {
    uint32_t vecSize = VarTypeByteSize(v.type) * v.columns;

    if(v.ColMajor())
      vecSize = VarTypeByteSize(v.type) * v.rows;

    for(uint32_t r = 0; r < v.rows; r++)
    {
      n->addChild(new RDTreeWidgetItem(
          {QFormatStr("%1.row%2").arg(v.name).arg(r), RowString(v, r), RowTypeString(v)}));
    }
  }
}

void BufferViewer::UI_AddTaskPayloads(RDTreeWidgetItem *root, size_t baseOffset,
                                      const rdcarray<ShaderConstant> &consts, BufferData *buffer)
{
  uint32_t offset = 0;

  for(size_t idx = 0; idx < consts.size(); idx++)
  {
    const ShaderConstant &c = consts[idx];
    ShaderVariable v = InterpretShaderVar(c, buffer->data() + baseOffset + offset, buffer->end());

    RDTreeWidgetItem *n = new RDTreeWidgetItem({v.name, VarString(v, c), TypeString(v, c)});

    root->addChild(n);

    TaskAddMatrixRows(n, c, v);

    // if it's an array the value (v) will be expanded with one element in each of v.members, but
    // the constant (c) will just have the type with a number of elements
    if(c.type.elements > 1)
    {
      ShaderConstant noarray = c;
      noarray.type.elements = 1;

      // calculate the tight scalar-packed advance, so we can detect padding
      uint32_t elSize = BufferFormatter::GetVarAdvance(PackingRules::Scalar(), noarray);

      for(uint32_t e = 0; e < v.members.size(); e++)
      {
        const uint32_t elOffset = (uint32_t)baseOffset + c.byteOffset + c.type.arrayByteStride * e;

        RDTreeWidgetItem *el = new RDTreeWidgetItem(
            {v.members[e].name, VarString(v.members[e], c), TypeString(v.members[e], c)});

        // if it's an array of structs we can recurse, just need to do the outer iteration here
        // because v.members[...].members will be the actual struct members because of the expansion
        if(c.type.baseType == VarType::Struct)
        {
          UI_AddTaskPayloads(el, elOffset, c.type.members, buffer);
        }
        else
        {
          // otherwise just expand by hand since there will be no more members in c.type.members for
          // us to recurse with
          TaskAddMatrixRows(el, c, v.members[e]);
        }

        n->addChild(el);

        // don't count the padding in the last struct in an array of structs, it will be handled as
        // padding after the array
        if(c.type.baseType == VarType::Struct && e + 1 == v.members.size())
          break;
      }
    }
    // for single structs, recurse
    else if(v.type == VarType::Struct)
    {
      UI_AddTaskPayloads(n, c.byteOffset, c.type.members, buffer);
    }

    // advance by the tight scalar-packed advance, so we can detect padding
    offset += BufferFormatter::GetVarAdvance(PackingRules::Scalar(), c);
  }
}

// Applies the ReplayLab alias for one reflected constant: the row shows the alias (or the
// original name when none is set) and remembers the original in the tag and tooltip. Nothing
// about the shader, its reflection or the extension callback identity changes.
static QString ApplyCBufferAlias(RDTreeWidgetItem *item, FixedVarTag &tag, const QString &original,
                                 const QString &path, bool leaf,
                                 const ReplayLabCBufferAliases &aliases)
{
  tag.path = path;
  tag.leaf = leaf;
  tag.alias.clear();
  tag.aliasSource.clear();
  if(!aliases.Loaded())
    return original;

  ReplayLabCBufferAliasKey key;
  key.path = path;
  key.offset = leaf ? (qint64)tag.byteOffset : -1;
  const ReplayLabCBufferAlias alias = aliases.Resolve(key);
  if(!alias.valid())
    return original;

  tag.alias = alias.alias;
  tag.aliasSource = alias.source;
  item->setText(0, alias.alias);
  if(alias.source == QStringLiteral("user"))
    item->setToolTip(0, QCoreApplication::translate("BufferViewer",
                                                    "Original name: %1 (alias set by you)")
                            .arg(original));
  else
    item->setToolTip(0, QCoreApplication::translate("BufferViewer",
                                                    "Original name: %1 (alias proposed by AI)")
                            .arg(original));
  return alias.alias;
}

void BufferViewer::UI_AddFixedVariables(RDTreeWidgetItem *root, uint32_t baseOffset,
                                        const rdcarray<ShaderConstant> &consts,
                                        const rdcarray<ShaderVariable> &vars,
                                        const QString &parentPath)
{
  const bool showPadding = ui->showPadding->isChecked() && m_CurCBuffer.bytesBacked;

  if(consts.size() != vars.size())
    qCritical() << "Shader variable mismatch";

  uint32_t offset = 0;

  for(size_t idx = 0; idx < consts.size() && idx < vars.size(); idx++)
  {
    const ShaderConstant &c = consts[idx];
    const ShaderVariable &v = vars[idx];

    if(showPadding && c.byteOffset > offset)
    {
      uint32_t size = c.byteOffset - offset;

      RDTreeWidgetItem *pad = new RDTreeWidgetItem({
          QString(),
          QFormatStr("%1 bytes").arg(Formatter::HumanFormat(size, Formatter::OffsetSize)),
          QString(),
          tr("Padding"),
      });

      pad->setItalic(true);
      pad->setTag(QVariant::fromValue(FixedVarTag(size)));

      root->addChild(pad);

      offset = c.byteOffset;
    }

    QVariant offsetStr = Formatter::HumanFormat(baseOffset + c.byteOffset, Formatter::OffsetSize);

    if(c.bitFieldSize != 0)
    {
      offsetStr =
          offsetStr.toString() +
          QFormatStr(" (bits %1:%2)").arg(c.bitFieldOffset).arg(c.bitFieldOffset + c.bitFieldSize);
    }

    if(m_CurCBuffer.compileConstants)
      offsetStr = lit("-");

    RDTreeWidgetItem *n =
        new RDTreeWidgetItem({v.name, VarString(v, c), offsetStr, TypeString(v, c)});

    // display colour swatch for floats with RGB display
    if((v.flags & ShaderVariableFlags::RGBDisplay) && VarTypeCompType(v.type) == CompType::Float &&
       v.rows == 1 && v.columns >= 1 && v.members.empty())
    {
      QColor swatchColor(0, 0, 0, 255);
      float rgb[3] = {0.0f, 0.0f, 0.0f};
      for(uint8_t col = 0; col < v.columns && col < 4; col++)
      {
        float fval = 0.0f;
        if(v.type == VarType::Float)
          fval = v.value.f32v[col];
        else if(v.type == VarType::Double)
          fval = float(v.value.f64v[col]);
        else if(v.type == VarType::Half)
          fval = float(v.value.f16v[col]);
        rgb[col] = ConvertLinearToSRGB(fval);
      }
      swatchColor.setRgbF(rgb[0], rgb[1], rgb[2], 1.0f);
      n->setIcon(1, MakeSwatchIcon(ui->fixedVars, swatchColor));
    }

    // i18n-ignore: "." joins reflected variable path segments, it is not user-facing prose.
    const QString varPath =
        parentPath.isEmpty() ? QString(v.name) : parentPath + lit(".") + QString(v.name);
    {
      FixedVarTag tag = ReplayLabFixedVarTag(c, v, baseOffset + c.byteOffset);
      const bool leaf = c.type.members.empty() && c.type.elements <= 1;
      ApplyCBufferAlias(n, tag, QString(v.name), varPath, leaf, m_CBufferAliases);
      n->setTag(QVariant::fromValue(tag));
    }

    root->addChild(n);

    UI_FixedAddMatrixRows(n, c, v);

    // if it's an array the value (v) will be expanded with one element in each of v.members, but
    // the constant (c) will just have the type with a number of elements
    if(c.type.elements > 1)
    {
      ShaderConstant noarray = c;
      noarray.type.elements = 1;

      // calculate the tight scalar-packed advance, so we can detect padding
      uint32_t elSize = BufferFormatter::GetVarAdvance(PackingRules::Scalar(), noarray);

      for(uint32_t e = 0; e < v.members.size(); e++)
      {
        const uint32_t elOffset = baseOffset + c.byteOffset + c.type.arrayByteStride * e;

        RDTreeWidgetItem *el = new RDTreeWidgetItem({
            v.members[e].name,
            VarString(v.members[e], c),
            m_CurCBuffer.compileConstants ? lit("-")
                                          : Formatter::HumanFormat(elOffset, Formatter::OffsetSize),
            TypeString(v.members[e], c),
        });

        // i18n-ignore: "." joins reflected variable path segments, it is not user-facing prose.
        const QString elPath = parentPath.isEmpty()
                                   ? QString(v.members[e].name)
                                   : parentPath + lit(".") + QString(v.members[e].name);
        {
          FixedVarTag tag = ReplayLabFixedVarTag(c, v.members[e], elOffset);
          const bool leaf = c.type.baseType != VarType::Struct;
          ApplyCBufferAlias(el, tag, QString(v.members[e].name), elPath, leaf, m_CBufferAliases);
          // an aliased array shows its alias on every element unless the element has its own
          if(tag.alias.isEmpty() && !n->tag().value<FixedVarTag>().alias.isEmpty())
          {
            const QString arrayAlias = n->tag().value<FixedVarTag>().alias;
            const QString elementName = QString(v.members[e].name);
            const int bracket = elementName.lastIndexOf(QLatin1Char('['));
            if(bracket >= 0)
              el->setText(0, arrayAlias + elementName.mid(bracket));
            el->setToolTip(0, tr("Original name: %1").arg(QString(v.members[e].name)));
          }
          el->setTag(QVariant::fromValue(tag));
        }

        // if it's an array of structs we can recurse, just need to do the outer iteration here
        // because v.members[...].members will be the actual struct members because of the expansion
        if(c.type.baseType == VarType::Struct)
        {
          UI_AddFixedVariables(el, elOffset, c.type.members, v.members[e].members, elPath);
        }
        else
        {
          // otherwise just expand by hand since there will be no more members in c.type.members for
          // us to recurse with
          UI_FixedAddMatrixRows(el, c, v.members[e]);
        }

        n->addChild(el);

        // don't count the padding in the last struct in an array of structs, it will be handled as
        // padding after the array
        if(c.type.baseType == VarType::Struct && e + 1 == v.members.size())
          break;

        if(showPadding && c.type.arrayByteStride > elSize)
        {
          uint32_t size = c.type.arrayByteStride - elSize;

          RDTreeWidgetItem *pad = new RDTreeWidgetItem({
              QString(),
              QFormatStr("%1 bytes").arg(Formatter::HumanFormat(size, Formatter::OffsetSize)),
              QString(),
              tr("Padding"),
          });

          pad->setItalic(true);
          pad->setTag(QVariant::fromValue(FixedVarTag(size)));

          n->addChild(pad);
        }
      }
    }
    // for single structs, recurse
    else if(v.type == VarType::Struct)
    {
      // Member offsets are relative to their containing struct. Preserve the full physical
      // CBuffer offset for nested structs so extension callback identities and the displayed
      // offset column refer to the same reflected byte.
      UI_AddFixedVariables(n, baseOffset + c.byteOffset, c.type.members, v.members, varPath);
    }

    // advance by the tight scalar-packed advance, so we can detect padding
    offset += BufferFormatter::GetVarAdvance(PackingRules::Scalar(), c);
  }
}

// Keep repeated semantic array roles readable without deleting an element identity. Only
// group AI rows when their physical paths prove the displayed array index. The full semantic
// name remains in the tag for search/attachments; every child retains its existing edit lanes.
static QList<RDTreeWidgetItem *> TakeCBufferChildren(RDTreeWidgetItem *parent)
{
  QList<RDTreeWidgetItem *> children;
  children.reserve(parent->childCount());
  // Removing from the end avoids shifting the entire child vector per row.
  while(parent->childCount())
    children.push_back(parent->takeChild(parent->childCount() - 1));
  std::reverse(children.begin(), children.end());
  return children;
}

static void AppendCBufferChildren(RDTreeWidgetItem *target, RDTreeWidgetItem *source)
{
  for(RDTreeWidgetItem *item : TakeCBufferChildren(source))
    target->addChild(item);
}

static void GroupCBufferSemanticArrays(RDTreeWidgetItem *root,
                                      const QList<RDTreeWidgetItem *> &projected)
{
  const QRegularExpression pattern(QStringLiteral("^([^\\[\\]]+)\\[(0|[1-9][0-9]*)\\](.*)$"));
  QMap<QString, QList<RDTreeWidgetItem *>> groups;
  QMap<QString, QSet<QString>> indices;
  QMap<RDTreeWidgetItem *, QString> labels;
  QMap<RDTreeWidgetItem *, RDTreeWidgetItem *> groupedParents;
  QSet<QString> existingNames;
  for(int i = 0; i < root->childCount(); ++i)
  {
    existingNames.insert(root->child(i)->text(0));
  }
  for(RDTreeWidgetItem *item : projected)
  {
    const FixedVarTag tag = item->tag().value<FixedVarTag>();
    if(tag.aliasSource == QLatin1String("user"))
      continue;
    const QRegularExpressionMatch match = pattern.match(item->text(0));
    if(!match.hasMatch())
      continue;
    const QString index = QLatin1Char('[') + match.captured(2) + QLatin1Char(']');
    bool physicalIndex = !tag.replayLabOriginalPaths.isEmpty();
    for(const QString &path : tag.replayLabOriginalPaths)
      physicalIndex &= path.contains(index);
    if(!physicalIndex)
      continue;
    const QString role = match.captured(1);
    groups[role].push_back(item);
    indices[role].insert(match.captured(2));
    labels[item] = match.captured(2) + match.captured(3);
  }
  for(auto group = groups.begin(); group != groups.end(); ++group)
  {
    // A single visible element needs no parent or code-style array decoration.
    // Its full alias and physical path remain searchable in the tag. Keep an
    // index visible when shortening would collide with another displayed name.
    if(group.value().size() == 1)
    {
      RDTreeWidgetItem *item = group.value().front();
      const QRegularExpressionMatch match = pattern.match(item->text(0));
      const QString shortened = group.key() + match.captured(3);
      if(!existingNames.contains(shortened))
      {
        item->setText(0, shortened);
        existingNames.insert(shortened);
      }
      continue;
    }
    // Multiple rows from one array element do not need another hierarchy level.
    // A pre-existing name takes precedence over a generated group.
    if(indices.value(group.key()).size() < 2 || existingNames.contains(group.key()))
      continue;
    RDTreeWidgetItem *parent = new RDTreeWidgetItem({group.key()});
    existingNames.insert(group.key());
    for(RDTreeWidgetItem *item : group.value())
      groupedParents[item] = parent;
  }
  if(groupedParents.isEmpty())
    return;
  // Reparent in the existing physical order once. Repeated indexOfChild/removal
  // scans made a large instance array quadratic even after value projection.
  for(RDTreeWidgetItem *item : TakeCBufferChildren(root))
  {
    RDTreeWidgetItem *parent = groupedParents.value(item, NULL);
    if(!parent)
    {
      root->addChild(item);
      continue;
    }
    if(!parent->parent())
      root->addChild(parent);
    item->setText(0, labels.value(item));
    parent->addChild(item);
  }
}

// Project already-decoded lanes rather than interpreting a synthetic packed declaration. This
// also handles fields that cross registers or consist of non-contiguous physical components.
struct CBufferSourceLane
{
  QString name;
  VarType type = VarType::Unknown;
  int column = 0, columns = 0;
};

static QMap<qint64, CBufferSourceLane> CBufferSourceNames(const rdcarray<ShaderConstant> &variables)
{
  QMap<qint64, CBufferSourceLane> lanes;
  bool valid = true;
  int visited = 0;
  std::function<void(const rdcarray<ShaderConstant> &, qint64, const QString &)> walk;
  walk = [&](const rdcarray<ShaderConstant> &members, qint64 base, const QString &parent) {
    for(const ShaderConstant &constant : members)
    {
      const uint32_t count = qMax(1U, constant.type.elements);
      if(count > 65536 || visited + count > 65536)
      {
        valid = false;
        return;
      }
      visited += count;
      const QString path = parent.isEmpty() ? QString(constant.name)
                                           : parent + QLatin1Char('.') + QString(constant.name);
      for(uint32_t element = 0; valid && element < count; element++)
      {
        const qint64 offset = base + constant.byteOffset +
                              qint64(element) * constant.type.arrayByteStride;
        const QString name = path + (count > 1 ? QStringLiteral("[%1]").arg(element) : QString());
        if(!constant.type.members.empty())
          walk(constant.type.members, offset, name);
        else if(constant.type.rows == 1 && constant.type.columns >= 1 && constant.type.columns <= 4 &&
                (constant.type.baseType == VarType::Float || constant.type.baseType == VarType::SInt ||
                 constant.type.baseType == VarType::UInt))
          for(int col = 0; col < constant.type.columns; col++)
          {
            const qint64 byte = offset + col * 4;
            if(lanes.contains(byte))
              valid = false;
            CBufferSourceLane lane;
            lane.name = name;
            lane.type = constant.type.baseType;
            lane.column = col;
            lane.columns = constant.type.columns;
            lanes[byte] = lane;
          }
      }
    }
  };
  walk(variables, 0, QString());
  return valid ? lanes : QMap<qint64, CBufferSourceLane>();
}

static int ApplyCBufferSemanticFields(RDTreeWidgetItem *root,
                                     const QList<ReplayLabCBufferSemanticField> &fields,
                                     const rdcarray<ShaderConstant> &displayVars = {})
{
  if(fields.isEmpty())
    return 0;
  struct Component
  {
    RDTreeWidgetItem *item = NULL;
    int column = 0;
  };
  QMap<qint64, Component> components;
  QSet<qint64> ambiguous;
  std::function<void(RDTreeWidgetItem *)> gather = [&](RDTreeWidgetItem *parent) {
    for(int i = 0; i < parent->childCount(); i++)
    {
      RDTreeWidgetItem *item = parent->child(i);
      const FixedVarTag tag = item->tag().value<FixedVarTag>();
      if(((tag.valid && tag.leaf && tag.replayLabMaterialLeaf) || tag.replayLabMatrixRow) &&
         tag.replayLabDisplayValue.rows == 1 && tag.replayLabComponents <= 4 &&
         tag.replayLabEditOffsets.size() == tag.replayLabComponents &&
         !(tag.replayLabDisplayValue.flags & ShaderVariableFlags::Truncated))
      {
        for(int col = 0; col < tag.replayLabComponents; col++)
        {
          const qint64 offset = qint64(tag.replayLabEditOffsets[col]);
          if(components.contains(offset))
            ambiguous.insert(offset);
          Component component;
          component.item = item;
          component.column = col;
          components[offset] = component;
        }
      }
      gather(item);
    }
  };
  gather(root);
  const auto sourceNames = CBufferSourceNames(displayVars);
  QMap<RDTreeWidgetItem *, QSet<int>> consumed;
  QList<RDTreeWidgetItem *> projected;
  for(const ReplayLabCBufferSemanticField &field : fields)
  {
    const VarType type = field.type.startsWith(QLatin1String("float")) ? VarType::Float
                         : field.type.startsWith(QLatin1String("uint")) ? VarType::UInt
                                                                       : VarType::SInt;
    bool valid = !field.offsets.isEmpty() && field.offsets.size() <= 4;
    for(qint64 offset : field.offsets)
    {
      if(!components.contains(offset) || ambiguous.contains(offset))
      {
        valid = false;
        break;
      }
      const Component component = components.value(offset);
      const FixedVarTag tag = component.item->tag().value<FixedVarTag>();
      bool userNamed = false;
      for(RDTreeWidgetItem *owner = component.item; owner && owner != root; owner = owner->parent())
        userNamed |= owner->tag().value<FixedVarTag>().aliasSource == QLatin1String("user");
      if(userNamed || tag.replayLabDisplayValue.type != type ||
         consumed.value(component.item).contains(component.column))
      {
        valid = false;
        break;
      }
    }
    if(!valid)
      continue;

    ShaderVariable value;
    value.name = field.name;
    value.type = type;
    value.rows = 1;
    value.columns = uint8_t(field.offsets.size());
    QStringList offsets;
    QStringList originals;
    for(int i = 0; i < field.offsets.size(); i++)
    {
      const qint64 offset = field.offsets[i];
      const Component component = components.value(offset);
      const FixedVarTag tag = component.item->tag().value<FixedVarTag>();
      value.value.u32v[i] = tag.replayLabDisplayValue.value.u32v[component.column];
      consumed[component.item].insert(component.column);
      offsets.push_back(QString::number(offset));
      QString path = tag.path;
      if(tag.replayLabComponents > 1)
        path += QLatin1Char('.') + QString(QLatin1Char("xyzw"[component.column]));
      originals.push_back(path);
    }
    QString displayName = field.name;
    const bool translatedAIName = field.source == QLatin1String("ai") &&
        std::any_of(field.name.begin(), field.name.end(), [](QChar c) { return c.unicode() > 127; });
    if(translatedAIName)
    {
      // Parameter identifiers are source data, independent of the UI language. Older AI
      // publications sometimes translated them. Prefer an existing English source identifier
      // when this field occupies one proven leaf; retain the AI alias in the tag/search/tooltip.
      const Component first = components.value(field.offsets.front());
      const FixedVarTag physical = first.item->tag().value<FixedVarTag>();
      const QString name = physical.path;
      static const QRegularExpression placeholder(
          QStringLiteral("(^|\\.)(_child[0-9]+|_m[0-9]+)(\\[|\\.|$)"));
      bool oneLeaf = !name.isEmpty() && !placeholder.match(name).hasMatch() &&
          std::all_of(name.begin(), name.end(), [](QChar c) { return c.unicode() < 128; });
      QString channels;
      for(qint64 offset : field.offsets)
      {
        const Component component = components.value(offset);
        oneLeaf &= component.item == first.item;
        channels += QLatin1Char("xyzw"[component.column]);
      }
      if(oneLeaf)
        displayName = name + (field.offsets.size() == physical.replayLabComponents
                                 ? QString() : QLatin1Char('.') + channels);
      // Vulkan and other APIs can retain captured placeholder reflection in the pipeline
      // panel after replacing its Shader. Use the exact current replacement's identifiers
      // only when every selected lane has the same source leaf and scalar type.
      const CBufferSourceLane source = sourceNames.value(field.offsets.front());
      bool sourceMatch = !source.name.isEmpty() && !placeholder.match(source.name).hasMatch() &&
          std::all_of(source.name.begin(), source.name.end(), [](QChar c) { return c.unicode() < 128; });
      channels.clear();
      for(qint64 offset : field.offsets)
      {
        const CBufferSourceLane lane = sourceNames.value(offset);
        sourceMatch &= lane.name == source.name && lane.type == type && lane.columns == source.columns;
        if(lane.column >= 0 && lane.column < 4)
          channels += QLatin1Char("xyzw"[lane.column]);
      }
      if(sourceMatch)
        displayName = source.name + (field.offsets.size() == source.columns
                                        ? QString() : QLatin1Char('.') + channels);
    }
    const QString status = field.status == QLatin1String("confirmed")
                               ? QCoreApplication::translate("BufferViewer", "Confirmed")
                               : QCoreApplication::translate("BufferViewer", "Inferred");
    QString provenance;
    if(field.provenance == QLatin1String("asset"))
      provenance = QCoreApplication::translate("BufferViewer", "Unpacked assets");
    else if(field.provenance == QLatin1String("combined"))
      provenance = QCoreApplication::translate("BufferViewer", "Unpacked assets and shader analysis");
    else
      provenance = QCoreApplication::translate("BufferViewer", "Shader analysis");
    FixedVarTag tag;
    tag.alias = field.name;
    tag.aliasSource = field.source;
    // Do not let a synthetic row become an extension leaf identity. Direct editing retains
    // the already validated lane mapping rather than guessing a new contiguous declaration.
    tag.byteOffset = uint32_t(*std::min_element(field.offsets.begin(), field.offsets.end()));
    tag.replayLabBaseType = type;
    tag.replayLabComponents = value.columns;
    tag.replayLabDisplayValue = value;
    tag.replayLabOriginalPaths = originals;
    for(qint64 offset : field.offsets)
      tag.replayLabEditOffsets.push_back(quint64(offset));
    tag.replayLabSemanticTooltip =
        QCoreApplication::translate("BufferViewer",
                                    "Display-only semantic field. The captured layout is unchanged.\n"
                                    "Status: %1\nSource: %2\nPhysical byte offsets: %3\n"
                                    "Original channels: %4\nDescription: %5\nEvidence: %6\n"
                                    "Source SHA-256: %7")
            .arg(status, provenance, offsets.join(QStringLiteral(", ")),
                 originals.join(QStringLiteral(", ")), field.description, field.evidence,
                 field.sourceSha256);
    if(displayName != field.name)
      tag.replayLabSemanticTooltip = field.name + QLatin1Char('\n') + tag.replayLabSemanticTooltip;
    ShaderConstant constant;
    constant.type.baseType = type;
    constant.type.rows = 1;
    constant.type.columns = value.columns;
    RDTreeWidgetItem *item = new RDTreeWidgetItem(
        {displayName, VarString(value, constant), offsets.join(QStringLiteral(", ")),
         field.type, status, provenance});
    item->setTag(QVariant::fromValue(tag));
    item->setToolTip(0, tag.replayLabSemanticTooltip);
    projected.push_back(item);
  }

  if(projected.isEmpty())
    return 0;

  QSet<RDTreeWidgetItem *> matrixParents, consumedParents;
  for(auto it = consumed.begin(); it != consumed.end(); ++it)
    consumedParents.insert(it.key()->parent());
  for(RDTreeWidgetItem *parent : consumedParents)
  {
    for(RDTreeWidgetItem *item : TakeCBufferChildren(parent))
    {
      auto used = consumed.constFind(item);
      if(used == consumed.constEnd())
      {
        parent->addChild(item);
        continue;
      }
      const FixedVarTag original = item->tag().value<FixedVarTag>();
      if(original.replayLabMatrixRow)
        matrixParents.insert(parent);
      // Split a partly consumed float4 into its untouched scalar channels. No unclaimed lane
      // disappears, and an unrelated parameter packed beside a color never inherits its name.
      for(int col = 0; col < original.replayLabComponents; col++)
      {
        if(used.value().contains(col))
          continue;
        ShaderVariable value = original.replayLabDisplayValue;
        value.columns = 1;
        value.value.u32v[0] = original.replayLabDisplayValue.value.u32v[col];
        FixedVarTag tag = original;
        tag.byteOffset = uint32_t(original.replayLabEditOffsets[col]);
        tag.replayLabComponents = 1;
        tag.replayLabDisplayValue = value;
        tag.replayLabMaterialLeaf = false;
        tag.replayLabEditOffsets = {quint64(tag.byteOffset)};
        tag.path += QLatin1Char('.') + QString(QLatin1Char("xyzw"[col]));
        tag.name = tag.path;
        ShaderConstant constant;
        constant.type.baseType = value.type;
        constant.type.rows = constant.type.columns = 1;
        RDTreeWidgetItem *remaining = new RDTreeWidgetItem(
            {tag.path, VarString(value, constant), QString::number(tag.byteOffset),
             TypeString(value, constant)});
        remaining->setTag(QVariant::fromValue(tag));
        parent->addChild(remaining);
      }
      delete item;
    }
  }
  for(RDTreeWidgetItem *matrix : matrixParents)
  {
    bool hasValues = false;
    for(int i = 0; i < matrix->childCount(); i++)
      hasValues |= !matrix->child(i)->tag().value<FixedVarTag>().padding;
    if(!hasValues && matrix->parent())
      delete matrix->parent()->takeChild(matrix->parent()->indexOfChild(matrix));
  }
  // Cache each offset once, then merge projected rows with the original rows.
  // Re-scanning the root and copying its value tags for each field is quadratic
  // and can stall the UI for buffers containing thousands of instance values.
  struct OrderedField
  {
    RDTreeWidgetItem *item;
    uint32_t offset;
  };
  QList<OrderedField> ordered;
  ordered.reserve(projected.size());
  for(RDTreeWidgetItem *item : projected)
    ordered.push_back({item, item->tag().value<FixedVarTag>().byteOffset});
  std::stable_sort(ordered.begin(), ordered.end(), [](const OrderedField &a, const OrderedField &b) {
    return a.offset < b.offset;
  });
  int next = 0;
  for(RDTreeWidgetItem *item : TakeCBufferChildren(root))
  {
    const FixedVarTag original = item->tag().value<FixedVarTag>();
    if(!original.padding && (original.valid || !original.replayLabSemanticTooltip.isEmpty()))
    {
      while(next < ordered.size() && ordered[next].offset < original.byteOffset)
        root->addChild(ordered[next++].item);
    }
    root->addChild(item);
  }
  while(next < ordered.size())
    root->addChild(ordered[next++].item);
  GroupCBufferSemanticArrays(root, projected);
  return projected.size();
}

static bool DescribeCBufferUnnamedFields(RDTreeWidgetItem *root,
                                         const QHash<qint64, QString> &unused)
{
  static const QRegularExpression placeholder(QStringLiteral("(^|\\.)(_child[0-9]+|_m[0-9]+)(\\.[xyzw])?$"));
  bool described = false;
  for(int i = 0; i < root->childCount(); i++)
  {
    RDTreeWidgetItem *item = root->child(i);
    const FixedVarTag tag = item->tag().value<FixedVarTag>();
    described |= DescribeCBufferUnnamedFields(item, unused);
    if(tag.padding || tag.replayLabEditOffsets.isEmpty() || tag.aliasSource == QLatin1String("user"))
      continue;
    const QString type = tag.replayLabBaseType == VarType::Float ? QStringLiteral("float")
                         : tag.replayLabBaseType == VarType::SInt ? QStringLiteral("int")
                         : tag.replayLabBaseType == VarType::UInt ? QStringLiteral("uint") : QString();
    bool allUnused = !type.isEmpty();
    for(quint64 offset : tag.replayLabEditOffsets)
      allUnused &= unused.value(qint64(offset)) == type;
    const bool unnamed = tag.alias.isEmpty() && placeholder.match(item->text(0)).hasMatch();
    if(!allUnused && !unnamed)
      continue;
    described = true;
    if(unnamed)
      item->setText(0, allUnused
                           ? QCoreApplication::translate("BufferViewer", "Unused · %1").arg(tag.byteOffset)
                           : QCoreApplication::translate("BufferViewer", "Parameter · %1").arg(tag.byteOffset));
    item->setText(4, allUnused
                        ? QCoreApplication::translate("BufferViewer", "Unused by this shader")
                        : QCoreApplication::translate("BufferViewer", "Meaning not identified"));
    const QString original = tag.path.isEmpty() ? QString(tag.name) : tag.path;
    item->setToolTip(0, allUnused
                           ? QCoreApplication::translate("BufferViewer", "Original name: %1\nThis shader does not read these components. Other shaders may use them; their bytes and editing controls are preserved.").arg(original)
                           : QCoreApplication::translate("BufferViewer", "Original name: %1\nThe role of this parameter has not been identified. This does not mean it is unused.").arg(original));
  }
  return described;
}

void BufferViewer::UI_ApplyCBufferSemanticFields(RDTreeWidgetItem *root,
                                                const rdcarray<ShaderConstant> &displayVars)
{
  QElapsedTimer projectionClock;
  projectionClock.start();
  const int projected = ApplyCBufferSemanticFields(root, m_CBufferAliases.SemanticFields(), displayVars);
  const bool described = DescribeCBufferUnnamedFields(root, m_CBufferAliases.UnusedComponents());
  setProperty("cbufferSemanticProjectionMs", projectionClock.elapsed());
  setProperty("cbufferSemanticProjectedRows", projected);
  if(projected > 0 || described)
    ui->fixedVars->setColumns(
        {tr("Name"), tr("Value"), tr("Byte Offset"), tr("Type"), tr("Status"), tr("Source")});
}

void BufferViewer::UI_LoadCBufferAliases()
{
  QElapsedTimer aliasClock;
  aliasClock.start();
  ui->fixedVars->setColumns({tr("Name"), tr("Value"), tr("Byte Offset"), tr("Type")});
  const ConstantBlock *block = NULL;
  const ShaderReflection *reflection = NULL;
  if(IsCBufferView())
  {
    reflection = CBufferReflection();
    if(reflection && m_CBufferSlot.slot < reflection->constantBlocks.size())
      block = &reflection->constantBlocks[m_CBufferSlot.slot];
  }
  const CBufferDebugDisplaySource displaySource = block && reflection
      ? m_Ctx.GetCBufferDebugDisplaySource(reflection->resourceId, m_CBufferSlot.stage,
                                           ReplayLabCurrentShaderRawSha256(reflection), *block)
      : CBufferDebugDisplaySource();
  const QString sourceIdentity = displaySource.sourceRawSha256.empty() ? QString()
      : QString(displaySource.sourceRawSha256) + QLatin1Char('|') +
            ReplayLabCBufferAliases::LayoutHash(displaySource.sourceBlock);
  // i18n-ignore: read-only native acceptance properties, not displayed labels.
  setProperty("cbufferDebugDisplaySourceSha256", QString(displaySource.sourceRawSha256));
  // User aliases are layout-wide. AI aliases for stripped placeholder reflection
  // additionally match this captured Shader and binding, preventing material names
  // from leaking to an unrelated Shader with the same unknown[N] rows.
  const QString identity = block && reflection
      ? ReplayLabCBufferAliases::StoreRoot() + QLatin1Char('|') +
            ReplayLabCBufferAliases::LayoutHash(*block) + QLatin1Char('|') +
            ReplayLabCurrentShaderRawSha256(reflection) + QLatin1Char('|') +
            ToQStr(reflection->resourceId) + QLatin1Char('|') +
            QString::number(int(m_CBufferSlot.stage)) + QLatin1Char('|') +
            QString::number(block->fixedBindNumber) + QLatin1Char('|') +
            QString::number(block->fixedBindSetOrSpace) + QLatin1Char('|') +
            QString::number(m_Ctx.IsResourceReplaced(reflection->resourceId) ? 1 : 0) +
            QLatin1Char('|') + sourceIdentity
      : QString();
  static const bool legacyRefresh =
      qEnvironmentVariableIntValue("RENDERDOC_CBUFFER_EDIT_LEGACY_REFRESH") == 1;
  const bool reuse = !legacyRefresh && m_CBufferAliases.Loaded() &&
                    !identity.isEmpty() && identity == m_CBufferAliasIdentity &&
                    !(m_CBufferAliasRefreshTimer && m_CBufferAliasRefreshTimer->isActive()) &&
                    CBufferAliasFileStamp(m_CBufferAliasFile) == m_CBufferAliasFileStamp;
  // Event/value refreshes preserve naming while the exact shader, project and
  // file revision are unchanged. Byte values are always decoded afresh.
  if(!reuse)
    ReplayLabLoadCBufferAliases(m_Ctx, m_CBufferAliases, block, reflection, m_CBufferSlot.stage,
                               &displaySource);
  setProperty("cbufferAliasCacheHit", reuse);
  m_CBufferAliasIdentity = identity;
  UI_UpdateCBufferAliasWatch();
  setProperty("cbufferAliasLoadMs", aliasClock.elapsed());
}

void BufferViewer::UI_UpdateCBufferAliasWatch()
{
  if(!m_CBufferAliasWatcher || !IsCBufferView())
    return;
  const QString path = m_CBufferAliases.Loaded()
                           ? QDir(ReplayLabCBufferAliases::StoreRoot())
                                 .absoluteFilePath(m_CBufferAliases.LayoutSha256() +
                                                   QStringLiteral(".json"))
                           : QString();
  if(path != m_CBufferAliasFile)
  {
    m_CBufferAliasRefreshTimer->stop();
    if(!m_CBufferAliasWatcher->files().isEmpty())
      m_CBufferAliasWatcher->removePaths(m_CBufferAliasWatcher->files());
    if(!m_CBufferAliasWatcher->directories().isEmpty())
      m_CBufferAliasWatcher->removePaths(m_CBufferAliasWatcher->directories());
    m_CBufferAliasFile = path;
  }
  m_CBufferAliasSequence = m_Sequence;
  m_CBufferAliasFileStamp = CBufferAliasFileStamp(path);
  if(path.isEmpty())
    return;
  QString directory = QFileInfo(path).absolutePath();
  while(!QDir(directory).exists())
  {
    const QString parent = QFileInfo(directory).absolutePath();
    if(parent == directory)
      break;
    directory = parent;
  }
  if(!m_CBufferAliasWatcher->directories().contains(directory) && QDir(directory).exists())
    m_CBufferAliasWatcher->addPath(directory);
  // QSaveFile/os.replace removes the old file watch. Reattach it after each atomic publication.
  if(!m_CBufferAliasWatcher->files().contains(path) && QFileInfo::exists(path))
    m_CBufferAliasWatcher->addPath(path);
}

void BufferViewer::UI_RefreshCBufferAliasDisplay()
{
  if(!IsCBufferView() || !m_Ctx.IsCaptureLoaded() || !m_Format.isEmpty() ||
     m_HasDeferredEvent || m_CBufferAliasSequence != m_Sequence)
    return;
  const BufferConfiguration &config = m_ModelIn->getConfig();
  // The model owns an immutable decoded snapshot of the displayed event. A rename is a pure
  // UI operation: do not repeat GetBufferData, replay, or compile just to update its labels.
  RDTreeViewExpansionState expanded;
  ui->fixedVars->saveExpansion(expanded, 0);
  const int scroll = ui->fixedVars->verticalScrollBar()->value();
  ui->fixedVars->beginUpdate();
  ui->fixedVars->clear();
  UI_InvalidateCBufferLiveEdit();
  // A requested alias refresh must read the newly published metadata even if
  // the filesystem exposes the same coarse timestamp and file size.
  m_CBufferAliasIdentity.clear();
  UI_LoadCBufferAliases();
  RDTreeWidgetItem stagedRows;
  UI_AddFixedVariables(&stagedRows, 0, config.fixedVars.type.members, config.evalVars);
  if(m_ShowCBufferSemanticFields && !m_CurCBuffer.compileConstants)
    UI_ApplyCBufferSemanticFields(&stagedRows, config.cbufferDisplayVars);
  if(!m_CurCBuffer.bytesBacked)
    UI_RemoveOffsets(&stagedRows);
  AppendCBufferChildren(ui->fixedVars->invisibleRootItem(), &stagedRows);
  ui->fixedVars->endUpdate();
  ui->fixedVars->applyExpansion(expanded, 0);
  ui->fixedVars->verticalScrollBar()->setValue(scroll);
  UI_UpdateCBufferSearch();
}

void BufferViewer::editCBufferAlias(const QString &path, const QString &original, qint64 offset,
                                    bool remove)
{
  ReplayLabCBufferAliasKey key;
  key.path = path;
  key.offset = offset;
  const ReplayLabCBufferAlias current = m_CBufferAliases.Resolve(key);

  QString alias;
  if(!remove)
  {
    if(!ReplayLabCBufferAliases::PromptAlias(this, original, current, alias))
      return;
  }

  QString error;
  const bool ok = alias.isEmpty() ? m_CBufferAliases.Remove(key, &error)
                                  : m_CBufferAliases.Set(key, alias, &error);

  if(!ok)
  {
    RDDialog::critical(this, tr("Constant Alias"), error);
    return;
  }

  OnEventChanged(CBufferEvent());
}

void BufferViewer::editCBufferBlockAlias(bool remove)
{
  const ShaderReflection *reflection =
      CBufferReflection();
  if(!reflection || m_CBufferSlot.slot >= reflection->constantBlocks.size())
    return;
  const ConstantBlock &block = reflection->constantBlocks[m_CBufferSlot.slot];
  ReplayLabLoadCBufferAliases(m_Ctx, m_CBufferAliases, &block, reflection, m_CBufferSlot.stage);
  if(!m_CBufferAliases.Loaded())
    return;

  QString alias;
  if(!remove && !ReplayLabCBufferAliases::PromptBlockAlias(
                    this, QString(block.name), m_CBufferAliases.BlockAlias(), alias))
    return;

  QString error;
  const bool ok = alias.isEmpty() ? m_CBufferAliases.RemoveBlockAlias(&error)
                                  : m_CBufferAliases.SetBlockAlias(alias, &error);
  if(!ok)
  {
    RDDialog::critical(this, tr("Constant Block Alias"), error);
    return;
  }

  OnEventChanged(CBufferEvent());
}

void BufferViewer::UI_RemoveOffsets(RDTreeWidgetItem *root)
{
  for(int i = 0; i < root->childCount(); i++)
  {
    RDTreeWidgetItem *item = root->child(i);
    item->setText(2, QVariant());
    UI_RemoveOffsets(item);
  }
}

void BufferViewer::calcBoundingData(CalcBoundingBoxData &bbox)
{
  for(size_t stage = 0; stage < ARRAY_COUNT(bbox.input); stage++)
  {
    const BufferConfiguration &s = bbox.input[stage];

    QList<FloatVector> &minOutputList = bbox.output.bounds[stage].Min;
    QList<FloatVector> &maxOutputList = bbox.output.bounds[stage].Max;

    minOutputList.reserve(s.columns.count());
    maxOutputList.reserve(s.columns.count());

    for(int i = 0; i < s.columns.count(); i++)
    {
      FloatVector maxvec(FLT_MAX, FLT_MAX, FLT_MAX, FLT_MAX);

      if(s.columns[i].type.columns == 1)
        maxvec.y = maxvec.z = maxvec.w = 0.0;
      else if(s.columns[i].type.columns == 2)
        maxvec.z = maxvec.w = 0.0;
      else if(s.columns[i].type.columns == 3)
        maxvec.w = 0.0;

      minOutputList.push_back(maxvec);
      maxOutputList.push_back(FloatVector(-maxvec.x, -maxvec.y, -maxvec.z, -maxvec.w));
    }

    QVector<CachedElData> cache;

    CacheDataForIteration(cache, s.columns, s.props, s.buffers, bbox.input[0].curInstance);

    // possible optimisation here if this shows up as a hot spot - sort and unique the indices and
    // iterate in ascending order, to be more cache friendly

    for(uint32_t row = 0; row < s.numRows; row++)
    {
      uint32_t idx = row;

      if(s.indices && s.indices->hasData())
      {
        idx = CalcIndex(s.indices, row, s.baseVertex, s.primRestart);

        if(idx == ~0U || (s.primRestart && idx == s.primRestart))
          continue;
      }

      for(int col = 0; col < s.columns.count(); col++)
      {
        const CachedElData &d = cache[col];
        const ShaderConstant *el = d.el;
        const BufferElementProperties *prop = d.prop;

        float *minOut = (float *)&minOutputList[col];
        float *maxOut = (float *)&maxOutputList[col];

        if(d.data)
        {
          const byte *bytes = d.data;

          if(!prop->perinstance)
            bytes += d.stride * idx;

          QVariantList list = GetVariants(prop->format, *el, bytes, d.end);

          for(int comp = 0; comp < 4 && comp < list.count(); comp++)
          {
            const QVariant &v = list[comp];

            QMetaType::Type vt = GetVariantMetatype(v);

            float fval = 0.0f;

            if(vt == QMetaType::Double)
              fval = (float)v.toDouble();
            else if(vt == QMetaType::Float)
              fval = v.toFloat();
            else if(vt == QMetaType::UInt || vt == QMetaType::UShort || vt == QMetaType::UChar)
              fval = (float)v.toUInt();
            else if(vt == QMetaType::Int || vt == QMetaType::Short || vt == QMetaType::SChar)
              fval = (float)v.toInt();
            else
              continue;

            if(qIsFinite(fval))
            {
              minOut[comp] = qMin(minOut[comp], fval);
              maxOut[comp] = qMax(maxOut[comp], fval);
            }
          }
        }
      }
    }
  }
}

void BufferViewer::UI_UpdateBoundingBox(const CalcBoundingBoxData &bbox)
{
  {
    QMutexLocker autolock(&m_BBoxLock);
    m_BBoxes[bbox.eventId] = bbox.output;
  }

  if(m_Ctx.CurEvent() == bbox.eventId)
  {
    UpdateCurrentMeshConfig();
    if(!m_MeshCameraInitialised)
      UI_ResetArcball();
    else
      QueueMeshPreview();
  }

  delete &bbox;
}

void BufferViewer::UI_UpdateBoundingBoxLabels(int compCount)
{
  if(compCount == 0)
  {
    BufferItemModel *model = currentBufferModel();
    if(model)
    {
      int posEl = model->posColumn();
      if(posEl >= 0 && posEl < model->getConfig().columns.count())
      {
        compCount = model->getConfig().columns[posEl].type.columns;
      }
    }
  }

  QString min, max;

  float *minData = &m_Config.minBounds.x;
  float *maxData = &m_Config.maxBounds.x;

  const QString comps = lit("xyzw");

  for(int i = 0; i < compCount && i < 4; i++)
  {
    if(i != 0)
    {
      min += lit("\n");
      max += lit("\n");
    }

    min += tr("Min %1: %2").arg(comps[i]).arg(Formatter::Format(minData[i]));
    max += tr("Max %1: %2").arg(comps[i]).arg(Formatter::Format(maxData[i]));
  }

  if(min.isEmpty())
    ui->minBoundsLabel->setText(lit("---"));
  else
    ui->minBoundsLabel->setText(min);

  if(max.isEmpty())
    ui->maxBoundsLabel->setText(lit("---"));
  else
    ui->maxBoundsLabel->setText(max);
}

void BufferViewer::UI_ResetArcball()
{
  BBoxData bbox;

  {
    QMutexLocker autolock(&m_BBoxLock);
    if(m_BBoxes.contains(m_Ctx.CurEvent()))
      bbox = m_BBoxes[m_Ctx.CurEvent()];
  }

  BufferItemModel *model = currentBufferModel();
  int stage = currentStageIndex();

  if(model)
  {
    int posEl = model->posColumn();
    if(posEl >= 0 && posEl < model->getConfig().columns.count() &&
       posEl < bbox.bounds[stage].Min.count())
    {
      FloatVector diag;
      diag.x = bbox.bounds[stage].Max[posEl].x - bbox.bounds[stage].Min[posEl].x;
      diag.y = bbox.bounds[stage].Max[posEl].y - bbox.bounds[stage].Min[posEl].y;
      diag.z = bbox.bounds[stage].Max[posEl].z - bbox.bounds[stage].Min[posEl].z;

      float len = qSqrt(diag.x * diag.x + diag.y * diag.y + diag.z * diag.z);

      if(diag.x >= 0.0f && diag.y >= 0.0f && diag.z >= 0.0f && len >= 1.0e-6f && len <= 1.0e+10f)
      {
        FloatVector mid;
        mid.x = bbox.bounds[stage].Min[posEl].x + diag.x * 0.5f;
        mid.y = bbox.bounds[stage].Min[posEl].y + diag.y * 0.5f;
        mid.z = bbox.bounds[stage].Min[posEl].z + diag.z * 0.5f;

        if(!isCurrentRasterOut())
        {
          // apply axis mapping to midpoint
          FloatVector transformedMid;
          transformedMid.x = m_Config.axisMapping.xAxis.x * mid.x +
                             m_Config.axisMapping.yAxis.x * mid.y +
                             m_Config.axisMapping.zAxis.x * mid.z;
          transformedMid.y = m_Config.axisMapping.xAxis.y * mid.x +
                             m_Config.axisMapping.yAxis.y * mid.y +
                             m_Config.axisMapping.zAxis.y * mid.z;
          transformedMid.z = m_Config.axisMapping.xAxis.z * mid.x +
                             m_Config.axisMapping.yAxis.z * mid.y +
                             m_Config.axisMapping.zAxis.z * mid.z;
          mid = transformedMid;
        }

        m_Arcball->Reset(mid, len * 0.7f);
        // Geometry arrives independently of bounds. Only a FOV-aware frame of
        // the current geometry is a completed initial camera placement.
        m_MeshCameraInitialised = false;
        if(m_UVData && m_UVData->geometry && m_UVDataSequence == m_Sequence &&
           m_UVEvent == m_Ctx.CurEvent())
          FocusMeshSelection(true);

        GUIInvoke::call(this, [this, len]() { ui->camSpeed->setValue(len / 200.0f); });
      }
    }
  }

  INVOKE_MEMFN(RT_UpdateAndDisplay);
}

void BufferViewer::UI_ConfigureFormats()
{
  if(!m_MeshView)
    return;

  const ActionDescription *action = m_Ctx.CurAction();

  if(action && (action->flags & ActionFlags::MeshDispatch))
    UI_ConfigureMeshPipeFormats();
  else
    UI_ConfigureVertexPipeFormats();
}

void BufferViewer::UI_ConfigureVertexPipeFormats()
{
  const PipeState &pipe = m_Ctx.CurPipelineState();

  rdcarray<BoundVBuffer> vbs = pipe.GetVBuffers();
  const ActionDescription *action = m_Ctx.CurAction();

  if(action)
  {
    m_InPosition = MeshFormat();
    m_InSecondary = MeshFormat();

    m_InPosition.allowRestart = pipe.IsRestartEnabled() && (action->flags & ActionFlags::Indexed);
    m_InPosition.restartIndex = pipe.GetRestartIndex();

    const BufferConfiguration &vsinConfig = m_ModelIn->getConfig();

    if(!vsinConfig.columns.empty())
    {
      int elIdx = m_ModelIn->posColumn();
      if(elIdx < 0 || elIdx >= vsinConfig.columns.count())
        elIdx = 0;

      if(vsinConfig.unclampedNumRows > 0)
        m_InPosition.numIndices = vsinConfig.numRows;
      else
        m_InPosition.numIndices = action->numIndices;

      if((action->flags & ActionFlags::Instanced) && action->numInstances == 0)
        m_InPosition.numIndices = 0;

      BoundVBuffer ib = pipe.GetIBuffer();
      m_InPosition.topology = pipe.GetPrimitiveTopology();
      m_InPosition.indexByteStride = ib.byteStride;
      m_InPosition.baseVertex = action->baseVertex;
      m_InPosition.indexResourceId = ib.resourceId;

      uint32_t drawIdxByteOffs = action->indexOffset * ib.byteStride;
      m_InPosition.indexByteOffset = ib.byteOffset + drawIdxByteOffs;
      if(ib.byteSize >= ~0U)
        m_InPosition.indexByteSize = ib.byteSize;
      else if(drawIdxByteOffs > ib.byteSize)
        m_InPosition.indexByteSize = 0;
      else
        m_InPosition.indexByteSize = ib.byteSize - drawIdxByteOffs;

      if((action->flags & ActionFlags::Indexed) && m_InPosition.indexByteStride == 0)
        m_InPosition.indexByteStride = 4U;

      {
        const ShaderConstant &el = vsinConfig.columns[elIdx];
        const BufferElementProperties &prop = vsinConfig.props[elIdx];

        m_InPosition.instanced = prop.perinstance;
        m_InPosition.instStepRate = prop.instancerate;

        if(prop.buffer < vbs.count() && !vsinConfig.genericsEnabled[elIdx])
        {
          m_InPosition.vertexResourceId = vbs[prop.buffer].resourceId;
          m_InPosition.vertexByteStride = vbs[prop.buffer].byteStride;
          m_InPosition.vertexByteOffset = vbs[prop.buffer].byteOffset + el.byteOffset +
                                          action->vertexOffset * m_InPosition.vertexByteStride;
          m_InPosition.vertexByteSize = vbs[prop.buffer].byteSize;
        }
        else
        {
          m_InPosition.vertexResourceId = ResourceId();
          m_InPosition.vertexByteStride = 0;
          m_InPosition.vertexByteOffset = 0;
        }

        m_InPosition.format = prop.format;
      }

      elIdx = m_ModelIn->secondaryColumn();

      if(elIdx >= 0 && elIdx < vsinConfig.columns.count())
      {
        const ShaderConstant &el = vsinConfig.columns[elIdx];
        const BufferElementProperties &prop = vsinConfig.props[elIdx];

        m_InSecondary.instanced = prop.perinstance;
        m_InSecondary.instStepRate = prop.instancerate;

        if(prop.buffer < vbs.count() && !vsinConfig.genericsEnabled[elIdx])
        {
          m_InSecondary.vertexResourceId = vbs[prop.buffer].resourceId;
          m_InSecondary.vertexByteStride = vbs[prop.buffer].byteStride;
          m_InSecondary.vertexByteOffset = vbs[prop.buffer].byteOffset + el.byteOffset +
                                           action->vertexOffset * m_InSecondary.vertexByteStride;
          m_InSecondary.vertexByteSize = vbs[prop.buffer].byteSize;
        }
        else
        {
          m_InSecondary.vertexResourceId = ResourceId();
          m_InSecondary.vertexByteStride = 0;
          m_InSecondary.vertexByteOffset = 0;
        }

        m_InSecondary.format = prop.format;
        m_InSecondary.showAlpha = m_ModelIn->secondaryAlpha();
      }
    }

    const BufferConfiguration &out1Config = m_ModelOut1->getConfig();

    m_Out1Position = MeshFormat();
    m_Out1Secondary = MeshFormat();

    if(!out1Config.columns.empty())
    {
      int elIdx = m_ModelOut1->posColumn();
      if(elIdx < 0 || elIdx >= out1Config.columns.count())
        elIdx = 0;

      const ShaderConstant &el = out1Config.columns[elIdx];
      const BufferElementProperties &prop = out1Config.props[elIdx];

      m_Out1Position = m_Out1Data;
      m_Out1Position.vertexByteOffset += el.byteOffset;
      m_Out1Position.unproject = prop.systemValue == ShaderBuiltin::Position;
      m_Out1Position.format.compCount = el.type.columns;

      // if geometry/tessellation is enabled, don't unproject VS output data
      if(m_Ctx.CurPipelineState().GetShader(ShaderStage::Tess_Eval) != ResourceId() ||
         m_Ctx.CurPipelineState().GetShader(ShaderStage::Geometry) != ResourceId())
        m_Out1Position.unproject = false;

      elIdx = m_ModelOut1->secondaryColumn();

      if(elIdx >= 0 && elIdx < out1Config.columns.count())
      {
        m_Out1Secondary = m_Out1Data;
        m_Out1Secondary.vertexByteOffset += out1Config.columns[elIdx].byteOffset;
        m_Out1Secondary.format = prop.format;
        m_Out1Secondary.showAlpha = m_ModelOut1->secondaryAlpha();
      }
    }

    m_Out1Position.allowRestart = m_InPosition.allowRestart;
    m_Out1Position.restartIndex = m_InPosition.restartIndex;

    const BufferConfiguration &out2Config = m_ModelOut2->getConfig();

    m_Out2Position = MeshFormat();
    m_Out2Secondary = MeshFormat();

    if(!out2Config.columns.empty())
    {
      int elIdx = m_ModelOut2->posColumn();
      if(elIdx < 0 || elIdx >= out2Config.columns.count())
        elIdx = 0;

      const ShaderConstant &el = out2Config.columns[elIdx];
      const BufferElementProperties &prop = out2Config.props[elIdx];

      m_Out2Position = m_Out2Data;
      m_Out2Position.vertexByteOffset += el.byteOffset;
      m_Out2Position.unproject = prop.systemValue == ShaderBuiltin::Position;

      elIdx = m_ModelOut2->secondaryColumn();

      if(elIdx >= 0 && elIdx < out2Config.columns.count())
      {
        m_Out2Secondary = m_Out2Data;
        m_Out2Secondary.vertexByteOffset += out2Config.columns[elIdx].byteOffset;
        m_Out2Secondary.showAlpha = m_ModelOut2->secondaryAlpha();
      }
    }

    m_Out2Position.allowRestart = false;

    m_Out2Position.indexByteStride = 0;

    if(!(action->flags & ActionFlags::Indexed))
      m_Out1Position.indexByteStride = m_InPosition.indexByteStride = 0;
  }
  else
  {
    m_InPosition = MeshFormat();
    m_InSecondary = MeshFormat();

    m_Out1Position = MeshFormat();
    m_Out1Secondary = MeshFormat();

    m_Out2Position = MeshFormat();
    m_Out2Secondary = MeshFormat();
  }
}

void BufferViewer::UI_ConfigureMeshPipeFormats()
{
  const PipeState &pipe = m_Ctx.CurPipelineState();

  const ActionDescription *action = m_Ctx.CurAction();

  m_InPosition = MeshFormat();
  m_InSecondary = MeshFormat();

  // out1 is task shaders, which do not have displayable data
  m_Out1Position = MeshFormat();
  m_Out1Secondary = MeshFormat();

  const BufferConfiguration &out2Config = m_ModelOut2->getConfig();

  m_Out2Position = MeshFormat();
  m_Out2Secondary = MeshFormat();
  m_Out2Position.allowRestart = false;

  if(!out2Config.columns.empty())
  {
    int elIdx = m_ModelOut2->posColumn();
    if(elIdx < 0 || elIdx >= out2Config.columns.count())
      elIdx = 0;

    const ShaderConstant &el = out2Config.columns[elIdx];
    const BufferElementProperties &prop = out2Config.props[elIdx];

    m_Out2Position = m_Out2Data;
    m_Out2Position.vertexByteOffset += el.byteOffset;
    m_Out2Position.unproject = prop.systemValue == ShaderBuiltin::Position;

    elIdx = m_ModelOut2->secondaryColumn();

    if(elIdx >= 0 && elIdx < out2Config.columns.count())
    {
      m_Out2Secondary = m_Out2Data;
      m_Out2Secondary.vertexByteOffset += out2Config.columns[elIdx].byteOffset;
      m_Out2Secondary.showAlpha = m_ModelOut2->secondaryAlpha();
    }
  }
}

void BufferViewer::configureDrawRange()
{
  const ActionDescription *action = m_Ctx.CurAction();

  int curIndex = ui->drawRange->currentIndex();

  bool instanced = true;

  // don't check the flags, check if there are actually multiple instances
  if(m_Ctx.IsCaptureLoaded())
    instanced = action && action->numInstances > 1;

  ui->drawRange->blockSignals(true);
  ui->drawRange->clear();
  if(instanced)
    ui->drawRange->addItems(
        {tr("This instance"), tr("Previous instances"), tr("All instances"), tr("Whole pass")});
  else
    ui->drawRange->addItems({tr("This draw"), tr("Previous instances (N/A)"),
                             tr("All instances (N/A)"), tr("Whole pass")});

  // preserve the previously selected index
  ui->drawRange->setCurrentIndex(qMax(0, curIndex));
  ui->drawRange->blockSignals(false);

  ui->drawRange->adjustSize();

  ui->drawRange->setEnabled(m_CurStage != MeshDataStage::VSIn);

  curIndex = ui->drawRange->currentIndex();

  m_Config.showPrevInstances = (curIndex >= 1);
  m_Config.showAllInstances = (curIndex >= 2);
  m_Config.showWholePass = (curIndex >= 3);
}

void BufferViewer::ApplyRowAndColumnDims(int numColumns, RDTableView *view, int dataColWidth)
{
  int start = 0;

  QList<int> widths;

  // vertex/element
  widths << m_IdxColWidth;

  // mesh view only - index
  if(m_MeshView)
    widths << m_IdxColWidth;

  for(int i = start; i < numColumns; i++)
    widths << dataColWidth;

  view->verticalHeader()->setDefaultSectionSize(m_DataRowHeight);

  view->setColumnWidths(widths);
}

void BufferViewer::UpdateCurrentMeshConfig()
{
  BBoxData bbox;

  uint32_t eventId = m_Ctx.CurEvent();

  {
    QMutexLocker autolocker(&m_BBoxLock);
    if(m_BBoxes.contains(eventId))
      bbox = m_BBoxes[eventId];
  }

  m_Config.type = m_CurStage;
  switch(m_CurStage)
  {
    case MeshDataStage::VSIn:
      m_Config.position = m_InPosition;
      m_Config.second = m_InSecondary;
      break;
    case MeshDataStage::VSOut:
      m_Config.position = m_Out1Position;
      m_Config.second = m_Out1Secondary;
      break;
    case MeshDataStage::GSOut:
    case MeshDataStage::MeshOut:
      m_Config.position = m_Out2Position;
      m_Config.second = m_Out2Secondary;
      break;
    case MeshDataStage::TaskOut:
    default: break;
  }

  UI_UpdateGuessParameters();

  m_Config.showBBox = false;

  if(m_CurStage == MeshDataStage::TaskOut)
    return;

  BufferItemModel *model = currentBufferModel();
  int stage = currentStageIndex();

  if(model)
  {
    int posEl = model->posColumn();
    if(posEl >= 0 && posEl < model->getConfig().columns.count() &&
       posEl < bbox.bounds[stage].Min.count())
    {
      m_Config.minBounds = bbox.bounds[stage].Min[posEl];
      m_Config.maxBounds = bbox.bounds[stage].Max[posEl];
      m_Config.showBBox = !isCurrentRasterOut();

      int compCount = model->getConfig().columns[posEl].type.columns;

      UI_UpdateBoundingBoxLabels(compCount);
    }
  }
}

void BufferViewer::render_mouseMove(QMouseEvent *e)
{
  if(!m_Ctx.IsCaptureLoaded())
    return;

  if(m_UVMeshSelecting && e->buttons() == Qt::LeftButton)
  {
    m_UVMeshDragged |=
        (e->pos() - m_UVMeshPress).manhattanLength() >= QApplication::startDragDistance();
    if(m_UVBox->isChecked())
    {
      if(m_UVMeshDragged)
      {
        m_UVMeshBox->setGeometry(QRect(m_UVMeshPress, e->pos()).normalized());
        m_UVMeshBox->show();
        m_UVMeshBox->raise();
      }
      return;
    }
    if(!m_UVMeshDragged)
      return;
  }

  if(m_CurrentCamera)
    m_CurrentCamera->MouseMove(e);

  if(e->buttons() & Qt::RightButton)
    render_clicked(e);

  // display if any mouse buttons are held while moving.
  if(e->buttons() != Qt::NoButton)
  {
    QueueMeshPreview();
  }
}

void BufferViewer::render_clicked(QMouseEvent *e)
{
  if(!m_Ctx.IsCaptureLoaded())
    return;

  QPoint curpos = e->pos();

  curpos *= ui->render->devicePixelRatioF();

  if((e->buttons() & Qt::RightButton) && m_Output &&
     (!m_MeshStack || m_MeshStack->currentWidget() == ui->render) &&
     meshCameraGesture(m_Ctx.Config().MeshViewer_CameraNavigation, e->buttons(), e->modifiers()) !=
         MeshCameraGesture::Dolly)
  {
    QPointer<BufferViewer> me(this);

    m_Ctx.Replay().AsyncInvoke(lit("PickVertex"), [this, me, curpos](IReplayController *r) {
      if(!me)
        return;

      uint32_t instanceSelected = 0;
      uint32_t vertSelected = 0;

      rdctie(vertSelected, instanceSelected) =
          m_Output->PickVertex((uint32_t)curpos.x(), (uint32_t)curpos.y());

      if(vertSelected != ~0U)
      {
        if(!me)
          return;

        GUIInvoke::call(this, [this, vertSelected, instanceSelected] {
          int row = (int)vertSelected;

          if(instanceSelected != m_Config.curInstance)
            ui->instance->setValue(instanceSelected);

          BufferItemModel *model = currentBufferModel();

          if(model && row >= 0 && row < model->rowCount())
            ScrollToRow(currentTable(), row);

          SyncViews(currentTable(), true, true);
        });
      }
    });
  }

  if(e->button() == Qt::LeftButton)
  {
    m_UVMeshSelecting = m_UVToggle && m_UVData->geometry &&
                        !m_UVData->geometry->triangles.isEmpty() && e->buttons() == Qt::LeftButton &&
                        !(e->modifiers() & (Qt::AltModifier | Qt::ControlModifier)) &&
                        m_Config.wireframeDraw;
    m_UVMeshPress = e->pos();
    m_UVMeshDragged = false;
  }

  if(m_MeshStack)
    m_MeshStack->currentWidget()->setFocus();
  else
    ui->render->setFocus();

  if(m_UVMeshSelecting && m_UVBox->isChecked())
    return;
  if(m_CurrentCamera)
    m_CurrentCamera->MouseClick(e);

  QueueMeshPreview();
}

void BufferViewer::render_unclicked(QMouseEvent *e)
{
  if(!m_Ctx.IsCaptureLoaded())
    return;

  if(m_UVMeshSelecting && e->button() == Qt::LeftButton)
  {
    m_UVMeshSelecting = false;
    m_UVMeshBox->hide();
    const bool extend = (e->modifiers() & Qt::ShiftModifier) != 0;
    const QSize size = m_MeshStack->size();
    if(m_UVBox->isChecked() && m_UVMeshDragged)
      m_UVCanvas->selectMeshBox(QRectF(m_UVMeshPress, e->localPos()).normalized(), size,
                                MeshPreviewTransform(), m_Config.position.unproject, extend);
    else if(!m_UVMeshDragged)
      m_UVCanvas->selectMesh(e->localPos(), size, MeshPreviewTransform(),
                             m_Config.position.unproject, extend);
  }

  if(m_CurrentCamera)
    m_CurrentCamera->MouseUnclick(e);

  QueueMeshPreview();
}

void BufferViewer::ScrollToRow(RDTableView *view, int row)
{
  int hs = view->horizontalScrollBar()->value();

  view->scrollTo(view->model()->index(row, 0), QAbstractItemView::PositionAtTop);
  view->clearSelection();
  view->selectRow(row);

  view->horizontalScrollBar()->setValue(hs);
}

void BufferViewer::ScrollToColumn(RDTableView *view, int column)
{
  int vs = view->verticalScrollBar()->value();

  view->scrollTo(view->model()->index(0, column), QAbstractItemView::PositionAtTop);

  view->verticalScrollBar()->setValue(vs);
}

void BufferViewer::ShowMeshData(MeshDataStage stage)
{
  const ActionDescription *action = m_Ctx.CurAction();
  if(action && (action->flags & ActionFlags::MeshDispatch) && stage == MeshDataStage::VSIn)
  {
    ToolWindowManager::raiseToolWindow(m_Containers[2]);
    return;
  }

  if(stage == MeshDataStage::VSIn)
    ToolWindowManager::raiseToolWindow(m_Containers[0]);
  else if(stage == MeshDataStage::VSOut)
    ToolWindowManager::raiseToolWindow(m_Containers[1]);
  else if(stage == MeshDataStage::GSOut)
    ToolWindowManager::raiseToolWindow(m_Containers[2]);
  else if(stage == MeshDataStage::TaskOut)
    ToolWindowManager::raiseToolWindow(m_Containers[0]);
  else if(stage == MeshDataStage::MeshOut)
    ToolWindowManager::raiseToolWindow(m_Containers[1]);
}

void BufferViewer::SetCurrentInstance(int32_t instance)
{
  if(ui->instance->isVisible() && ui->instance->isEnabled())
    ui->instance->setValue(instance);
}

void BufferViewer::SetCurrentView(int32_t view)
{
  if(ui->viewIndex->isVisible() && ui->viewIndex->isEnabled())
    ui->viewIndex->setValue(view);
}

void BufferViewer::SetPreviewStage(MeshDataStage stage)
{
  if(m_MeshView)
  {
    if(stage == MeshDataStage::VSIn)
      ui->outputTabs->setCurrentIndex(0);
    else if(stage == MeshDataStage::VSOut)
      ui->outputTabs->setCurrentIndex(1);
    else if(stage == MeshDataStage::GSOut)
      ui->outputTabs->setCurrentIndex(2);
    else if(stage == MeshDataStage::TaskOut)
      ui->outputTabs->setCurrentIndex(1);
    else if(stage == MeshDataStage::MeshOut)
      ui->outputTabs->setCurrentIndex(2);
  }
}

void BufferViewer::ViewBuffer(uint64_t byteOffset, uint64_t byteSize, ResourceId id,
                              const rdcstr &format)
{
  if(!m_Ctx.IsCaptureLoaded())
    return;

  m_IsBuffer = true;
  m_ByteOffset = byteOffset;
  m_ByteSize = byteSize;
  m_BufferID = id;
  m_TexSub = {0, 0, 0};

  updateLabelsAndLayout();

  BufferDescription *buf = m_Ctx.GetBuffer(id);
  if(buf)
    m_ObjectByteSize = buf->length;

  m_PagingByteOffset = 0;

  ui->formatSpecifier->setAutoFormat(format);
}

BufferViewer *BufferViewer::HasCBufferView(ShaderStage stage, uint32_t slot, uint32_t idx)
{
  CBufferSlot cbuffer = {stage, slot, idx};

  for(BufferViewer *c : m_CBufferViews)
  {
    if(c->m_CBufferSlot == cbuffer && !c->m_CBufferPinnedReflection)
      return c;
  }

  return NULL;
}

BufferViewer *BufferViewer::GetFirstCBufferView(BufferViewer *exclude)
{
  for(BufferViewer *b : m_CBufferViews)
  {
    if(b != exclude && !b->m_CBufferPinnedReflection)
      return b;
  }

  return NULL;
}

void BufferViewer::ViewCBuffer(const ShaderStage stage, uint32_t slot, uint32_t idx)
{
  if(!m_Ctx.IsCaptureLoaded())
    return;

  m_IsBuffer = true;
  m_ByteOffset = 0;
  m_ByteSize = UINT64_MAX;
  m_BufferID = ResourceId();
  m_CBufferSlot = {stage, slot, idx};
  m_CBufferPinnedReflection.reset();
  m_CBufferPinnedEvent = m_CBufferDisplayedEvent = 0;
  if(m_CBufferPin)
  {
    const QSignalBlocker blocker(m_CBufferPin);
    m_CBufferPin->setChecked(false);
    m_CBufferPin->setEnabled(false);
    m_CBufferPin->show();
  }
  if(m_CBufferSearch)
    m_CBufferSearch->show();
  if(m_CBufferLiveEdit)
    m_CBufferLiveEdit->panel->show();
  m_TexSub = {0, 0, 0};

  updateLabelsAndLayout();

  m_ObjectByteSize = 0;
  m_PagingByteOffset = 0;

  // enable the button to toggle on formatting, so we can pre-fill with a sensible format when it's
  // enabled
  ui->setFormat->setVisible(true);

  ui->formatSpecifier->setFormat(QString());
  ui->formatSpecifier->setVisible(false);
  ui->formatSpecifier->setAutoFormat(QString());

  m_CBufferViews.push_back(this);
}

void BufferViewer::ViewTexture(ResourceId id, const Subresource &sub, const rdcstr &format)
{
  if(!m_Ctx.IsCaptureLoaded())
    return;

  m_IsBuffer = false;
  m_ByteOffset = 0;
  m_ByteSize = UINT64_MAX;
  m_BufferID = id;
  m_TexSub = sub;

  updateLabelsAndLayout();

  TextureDescription *tex = m_Ctx.GetTexture(id);
  if(tex)
  {
    m_ObjectByteSize = tex->byteSize;

    if(m_TexSub.sample == ~0U)
      m_TexSub.sample = tex->msSamp - 1;
  }

  m_PagingByteOffset = 0;

  ui->formatSpecifier->setAutoFormat(format);
}

void BufferViewer::ScrollToRow(int32_t row, MeshDataStage stage)
{
  if(m_MeshView && stage == MeshDataStage::TaskOut)
  {
    ui->fixedVars->scrollToItem(ui->fixedVars->topLevelItem(row));
    ui->fixedVars->setSelectedItem(ui->fixedVars->topLevelItem(row));
    return;
  }

  if(!m_MeshView)
    stage = MeshDataStage::VSIn;

  ScrollToRow(tableForStage(stage), row);

  if(m_MeshView)
    m_Scroll[(int)stage].setY(row);
  else
    // the row scroll is visible and handles paging in the non-mesh view, so use it
    ui->rowOffset->setValue(row);
}

void BufferViewer::ScrollToColumn(int32_t column, MeshDataStage stage)
{
  if(!m_MeshView)
    stage = MeshDataStage::VSIn;

  ScrollToColumn(tableForStage(stage), column);

  m_Scroll[(int)stage].setX(column);
}

bool BufferViewer::eventFilter(QObject *watched, QEvent *event)
{
  if(m_MeshView && m_MeshStack)
  {
    const auto type = event->type();
    if((type == QEvent::KeyPress || type == QEvent::ShortcutOverride) && isVisible() &&
       !QApplication::activePopupWidget() && !QApplication::activeModalWidget())
    {
      QKeyEvent *key = static_cast<QKeyEvent *>(event);
      const auto mods = key->modifiers() & ~Qt::KeypadModifier;
      const int code = key->key();
      const bool focus = code == Qt::Key_F && mods == Qt::NoModifier;
      const bool selection =
          m_UVToggle && m_UVToggle->isChecked() &&
          ((mods == Qt::NoModifier && (code == Qt::Key_A || code == Qt::Key_L || code == Qt::Key_B ||
                                       (code >= Qt::Key_1 && code <= Qt::Key_3))) ||
           (mods == Qt::AltModifier && code == Qt::Key_A));
      if(focus || selection)
      {
        const QPoint cursor = QCursor::pos();
        QWidget *mesh = m_MeshStack->currentWidget();
        const QWidget *hovered = QApplication::widgetAt(cursor);
        const auto underCursor = [cursor, hovered](const QWidget *viewport) {
          return viewport && viewport->isVisible() &&
                 viewport->rect().contains(viewport->mapFromGlobal(cursor)) &&
                 (!hovered || viewport->isAncestorOf(hovered) || hovered == viewport ||
                  hovered->isAncestorOf(viewport));
        };
        const bool uv = underCursor(m_UVCanvas);
        if(uv || underCursor(mesh))
        {
          if(type == QEvent::KeyPress && !key->isAutoRepeat())
          {
            if(focus)
            {
              if(uv)
                m_UVCanvas->focusSelection();
              else
                FocusMeshSelection();
            }
            else if(code == Qt::Key_L && !uv)
              m_UVCanvas->selectMeshLinked(mesh->mapFromGlobal(cursor), m_MeshStack->size(),
                                           MeshPreviewTransform(), m_Config.position.unproject);
            else
              m_UVCanvas->handleSelectionShortcut(key);
          }
          key->accept();
          return true;
        }
      }
    }
  }
  if(m_UVTextureView && watched == m_UVTextureView)
  {
    switch(event->type())
    {
      case QEvent::MouseButtonPress: render_clicked(static_cast<QMouseEvent *>(event)); return true;
      case QEvent::MouseButtonRelease:
        render_unclicked(static_cast<QMouseEvent *>(event));
        return true;
      case QEvent::MouseMove: render_mouseMove(static_cast<QMouseEvent *>(event)); return true;
      case QEvent::Wheel: render_mouseWheel(static_cast<QWheelEvent *>(event)); return true;
      case QEvent::KeyPress: render_keyPress(static_cast<QKeyEvent *>(event)); return true;
      case QEvent::KeyRelease: render_keyRelease(static_cast<QKeyEvent *>(event)); return true;
      case QEvent::Resize: QueueMeshPreview(); break;
      default: break;
    }
  }
  if(watched == m_CBufferSearch && event->type() == QEvent::KeyPress &&
     static_cast<QKeyEvent *>(event)->key() == Qt::Key_Escape)
  {
    m_CBufferSearch->clear();
    ui->fixedVars->setFocus();
    return true;
  }
  if(event->type() == QEvent::ToolTip)
  {
    RDTreeWidget *tree = qobject_cast<RDTreeWidget *>(watched);
    if(tree)
    {
      RDTreeWidgetItem *item = tree->itemAt(tree->viewport()->mapFromGlobal(QCursor::pos()));
      if(item)
      {
        FixedVarTag tag = item->tag().value<FixedVarTag>();

        QString tooltip;

        if(!tag.replayLabSemanticTooltip.isEmpty())
        {
          QToolTip::showText(QCursor::pos() + QPoint(10, 10), tag.replayLabSemanticTooltip);
          return true;
        }

        PackingRules pack = m_ModelIn->getConfig().packing;

        if(tag.valid && tag.padding)
        {
          tooltip = tr("%1 bytes of padding. Packing rules in effect:\n\n")
                        .arg(Formatter::HumanFormat(tag.byteSize, Formatter::OffsetSize));

          if(pack == PackingRules::D3DCB())
            tooltip += tr("Standard D3D constant buffer packing.\n\n");
          else if(pack == PackingRules::STD140())
            tooltip += tr("Standard std140 buffer packing.\n\n");
          else if(pack == PackingRules::STD430())
            tooltip += tr("Standard std430 buffer packing.\n\n");
          else if(pack == PackingRules::C())
            tooltip += tr("Standard C / D3D UAV packing.\n\n");
          else if(pack == PackingRules::Scalar())
            tooltip += tr("Scalar packing.\n\n");

          if(pack.vectorAlignComponent)
            tooltip +=
                tr("- Vectors are only aligned to their component (float4 to 4-byte boundary)\n");
          else
            tooltip +=
                tr("- 3- and 4-wide vectors must be aligned to a 4-wide boundary\n"
                   "  (vec3 and vec4 to 16-byte boundary)\n");

          if(pack.tightArrays)
            tooltip += tr("- Arrays are tightly packed to each element\n");
          else
            tooltip += tr("- Arrays have a stride of a 16 bytes\n");

          if(pack.trailingOverlap)
            tooltip += tr("- Variables can overlap the trailing padding in arrays or structs.\n");
          else
            tooltip +=
                tr("- Variables must not overlap the trailing padding in arrays or structs.\n");

          if(pack.vectorStraddle16b)
            tooltip += tr("- Vectors can straddle 16-byte boundaries.\n");
          else
            tooltip += tr("- Vectors must not straddle 16-byte boundaries.\n");
        }
        else if(tag.valid && !tag.padding)
        {
          tooltip = tr("Variable %1 is at byte offset %2")
                        .arg(tag.name)
                        .arg(Formatter::HumanFormat(tag.byteOffset, Formatter::OffsetSize));

          if(!IsCBufferView())
            tooltip += tr(", not including overall base byte offset %1 in buffer")
                           .arg(Formatter::HumanFormat(m_ByteOffset, Formatter::OffsetSize));

          tooltip += lit(".");

          if(tag.matrix)
          {
            tooltip += tr("\n\nMatrix stored ");
            if(tag.rowmajor)
              tooltip += tr("row-major.");
            else
              tooltip += tr("column-major.");
          }
        }

        if(!tooltip.isEmpty())
        {
          QPoint pos = QCursor::pos();
          pos.setX(pos.x() + 10);
          pos.setY(pos.y() + 10);
          QToolTip::showText(pos, tooltip.trimmed());

          return true;
        }
      }
    }
    else if(!m_MeshView && watched == ui->inTable->viewport())
    {
      QModelIndex index =
          ui->inTable->indexAt(ui->inTable->viewport()->mapFromGlobal(QCursor::pos()));

      if(index.isValid())
      {
        const ShaderConstant &c = m_ModelIn->elementForColumn(index.column());

        QModelIndex rowidx = m_ModelIn->index(index.row(), 0, index.parent());
        int row = m_ModelIn->data(rowidx).toInt();

        size_t stride = m_ModelIn->getConfig().buffers[0]->stride;

        QString tooltip;

        tooltip =
            tr("%1 at overall byte offset %2")
                .arg(c.name)
                .arg(Formatter::HumanFormat(stride * row + c.byteOffset, Formatter::OffsetSize));
        tooltip += tr(", not including overall base byte offset %1 in buffer")
                       .arg(Formatter::HumanFormat(m_ByteOffset, Formatter::OffsetSize));

        tooltip += lit(".\n\n");

        tooltip +=
            tr("Row %1 begins at offset %2 (stride of %3 bytes)\n%4 is at offset %5 in each row.")
                .arg(row)
                .arg(Formatter::HumanFormat(stride * row, Formatter::OffsetSize))
                .arg(Formatter::HumanFormat(stride, Formatter::OffsetSize))
                .arg(c.name)
                .arg(Formatter::HumanFormat(c.byteOffset, Formatter::OffsetSize));

        QPoint pos = QCursor::pos();
        pos.setX(pos.x() + 10);
        pos.setY(pos.y() + 10);
        QToolTip::showText(pos, tooltip.trimmed());

        return true;
      }
    }
  }
  else if(!m_MeshView && watched == ui->inTable->viewport())
  {
    if(event->type() == QEvent::MouseMove)
    {
      bool ret = QObject::eventFilter(watched, event);

      QMouseEvent *mouseEvent = (QMouseEvent *)event;

      if(m_delegate->linkHover(mouseEvent, font(),
                               ui->inTable->indexAt(mouseEvent->localPos().toPoint())))
        ui->inTable->setCursor(QCursor(Qt::PointingHandCursor));
      else
        ui->inTable->unsetCursor();

      return ret;
    }
  }

  return QObject::eventFilter(watched, event);
}

void BufferViewer::updateLabelsAndLayout()
{
  if(m_MeshView)
  {
    setWindowTitle(tr("Mesh Viewer"));

    if(m_Ctx.IsCaptureLoaded())
    {
      GraphicsAPI pipeType = m_Ctx.APIProps().pipelineType;

      if(isMeshDraw())
      {
        m_Containers[0]->layout()->addWidget(ui->out1Table);
        m_Containers[0]->layout()->addWidget(ui->fixedVars);
        m_Containers[1]->layout()->addWidget(ui->out2Table);
        m_Containers[2]->layout()->addWidget(ui->inTable);

        ui->instanceLabel->setVisible(false);
        ui->instance->setVisible(false);
        ui->meshFilterLabel->setVisible(true);
        ui->resetMeshFilterButton->setVisible(true);

        ui->fixedVars->setVisible(true);
        ui->out1Table->setVisible(false);
        m_Containers[2]->setWindowTitle(tr("Mesh Input"));
        m_Containers[0]->setWindowTitle(IsD3D(pipeType) ? tr("Amp. Out") : tr("Task Out"));
        m_Containers[1]->setWindowTitle(tr("Mesh Output"));

        if(ui->outputTabs->indexOf(ui->out1Tab) == 1)
          ui->outputTabs->removeTab(1);
        ui->outputTabs->setTabText(0, tr("Mesh Input"));
        ui->outputTabs->setTabText(1, tr("Mesh Out"));

        if(ui->visualisation->findText(tr("Meshlet")) < 0)
          ui->visualisation->insertItem(int(Visualisation::Meshlet), tr("Meshlet"));
        ui->visualisation->adjustSize();
      }
      else
      {
        m_Containers[0]->layout()->addWidget(ui->inTable);
        m_Containers[0]->layout()->addWidget(ui->fixedVars);
        m_Containers[1]->layout()->addWidget(ui->out1Table);
        m_Containers[2]->layout()->addWidget(ui->out2Table);

        ui->instanceLabel->setVisible(true);
        ui->instance->setVisible(true);
        ui->meshFilterLabel->setVisible(false);
        ui->resetMeshFilterButton->setVisible(false);

        ui->fixedVars->setVisible(false);
        ui->out1Table->setVisible(true);
        m_Containers[0]->setWindowTitle(tr("VS Input"));
        m_Containers[1]->setWindowTitle(tr("VS Output"));
        m_Containers[2]->setWindowTitle(tr("GS/DS Output"));

        ui->outputTabs->setTabText(0, tr("VS In"));
        if(ui->outputTabs->indexOf(ui->out1Tab) < 0)
          ui->outputTabs->insertTab(1, ui->out1Tab, tr("VS Out"));
        ui->outputTabs->setTabText(1, tr("VS Out"));
        ui->outputTabs->setTabText(2, tr("GS/DS Out"));

        const int meshlet = ui->visualisation->findText(tr("Meshlet"));
        if(meshlet >= 0)
          ui->visualisation->removeItem(meshlet);
        ui->visualisation->adjustSize();
      }
    }
    else
    {
      m_Containers[0]->layout()->addWidget(ui->inTable);
      m_Containers[0]->layout()->addWidget(ui->fixedVars);
      m_Containers[1]->layout()->addWidget(ui->out1Table);
      m_Containers[2]->layout()->addWidget(ui->out2Table);

      ui->instanceLabel->setVisible(true);
      ui->instance->setVisible(true);
      ui->meshFilterLabel->setVisible(false);
      ui->resetMeshFilterButton->setVisible(false);

      ui->fixedVars->setVisible(false);
      ui->out1Table->setVisible(true);
      m_Containers[0]->setWindowTitle(tr("VS Input"));
      m_Containers[1]->setWindowTitle(tr("VS Output"));
      m_Containers[2]->setWindowTitle(tr("GS/DS Output"));

      ui->outputTabs->setTabText(0, tr("VS In"));
      if(ui->outputTabs->indexOf(ui->out1Tab) < 0)
        ui->outputTabs->insertTab(1, ui->out1Tab, tr("VS Out"));
      ui->outputTabs->setTabText(1, tr("VS Out"));
      ui->outputTabs->setTabText(2, tr("GS/DS Out"));

      const int meshlet = ui->visualisation->findText(tr("Meshlet"));
      if(meshlet >= 0)
        ui->visualisation->removeItem(meshlet);
      ui->visualisation->adjustSize();
    }
  }
  else
  {
    if(IsCBufferView())
    {
      QString bufName;

      const ShaderReflection *reflection =
          CBufferReflection();

      uint32_t arraySize = ~0U;
      const ConstantBlock *aliasBlock = NULL;
      if(reflection != NULL && m_CBufferSlot.slot < reflection->constantBlocks.size())
        aliasBlock = &reflection->constantBlocks[m_CBufferSlot.slot];

      ReplayLabLoadCBufferAliases(m_Ctx, m_CBufferAliases, aliasBlock, reflection,
                                  m_CBufferSlot.stage);
      if(aliasBlock != NULL)
      {
        if(!aliasBlock->name.isEmpty())
        {
          const ReplayLabCBufferAlias blockAlias = m_CBufferAliases.BlockAlias();
          bufName = QFormatStr("<%1>").arg(blockAlias.valid() ? blockAlias.alias
                                                              : QString(aliasBlock->name));
          arraySize = aliasBlock->bindArraySize;
        }
      }

      if(bufName.isEmpty())
      {
        if(m_BufferID != ResourceId())
          bufName = m_Ctx.GetResourceName(m_BufferID);
        else
          bufName = tr("Unbound");
      }

      GraphicsAPI pipeType = m_Ctx.APIProps().pipelineType;

      QString title = QFormatStr("%1 %2 %3")
                          .arg(ToQStr(m_CBufferSlot.stage, pipeType))
                          .arg(IsD3D(pipeType) ? lit("CB") : lit("UBO"))
                          .arg(m_CBufferSlot.slot);

      if(m_Ctx.CurPipelineState().SupportsResourceArrays() && arraySize > 1)
        title += QFormatStr("[%1]").arg(m_CBufferSlot.arrayIdx);

      title += QFormatStr(" - %1").arg(bufName);
      if(m_CBufferPinnedReflection)
        title = tr("%1 (Pinned: EID %2)").arg(title).arg(m_CBufferPinnedEvent);

      setWindowTitle(title);
    }
    else
    {
      setWindowTitle(m_Ctx.GetResourceName(m_BufferID) + lit(" - Contents"));
    }
  }
}

void BufferViewer::on_resourceDetails_clicked()
{
  if(m_BufferID == ResourceId())
    return;

  if(!m_Ctx.HasResourceInspector())
    m_Ctx.ShowResourceInspector();

  m_Ctx.GetResourceInspector()->Inspect(m_BufferID);

  ToolWindowManager::raiseToolWindow(m_Ctx.GetResourceInspector()->Widget());
}

void BufferViewer::render_mouseWheel(QWheelEvent *e)
{
  if(m_CurrentCamera)
  {
    m_CurrentCamera->MouseWheel(e);
    m_MeshCameraInitialised = true;
  }

  QueueMeshPreview();
}

void BufferViewer::render_keyPress(QKeyEvent *e)
{
  if(m_UVToggle && m_UVToggle->isChecked() && m_CurrentCamera == m_Arcball &&
     m_UVCanvas->handleSelectionShortcut(e))
    return;
  m_CurrentCamera->KeyDown(e);
}

void BufferViewer::render_keyRelease(QKeyEvent *e)
{
  m_CurrentCamera->KeyUp(e);
}

void BufferViewer::render_timer()
{
  if(m_CurrentCamera &&
     m_CurrentCamera->Update(m_MeshStack ? m_MeshStack->rect() : ui->render->rect()))
    QueueMeshPreview();
}

void BufferViewer::RT_UpdateAndDisplay(IReplayController *)
{
  if(m_Output)
  {
    MeshDisplay config = m_Config;
    config.cam = m_CurrentCamera->camera();
    const auto highlight = std::atomic_load(&m_UVHighlight);
    if(!config.wireframeDraw)
      config.highlightVert = ~0U;
    if(config.wireframeDraw && highlight && !highlight->positions.empty())
    {
      if(highlight->selectedVertices > 0)
        config.highlightVert = ~0U;
      RENDERDOC_SetMeshComponentOverlay(m_Output, highlight->positions.data(),
                                        uint32_t(highlight->positions.size()), highlight->topology,
                                        highlight->selectedVertices);
    }
    else
      RENDERDOC_SetMeshComponentHighlight(m_Output, nullptr, 0, Topology::Unknown);
    m_Output->SetMeshDisplay(config);
  }

  GUIInvoke::call(this, [this]() {
    if(m_UVTextureView && m_MeshStack->currentWidget() == m_UVTextureContainer)
      m_UVTextureView->setTransform(MeshPreviewTransform(), m_Config.position.unproject,
                                    m_Config.wireframeDraw, m_Ctx.Config().MeshViewer_CameraNear);
    else
      ui->render->update();
  });
}

QPushButton *BufferViewer::MakePreviousPageButton()
{
  QPushButton *b = new QPushButton(tr("Prev Page"), this);
  QObject::connect(b, &QPushButton::clicked, [this] {
    int page = ui->rowOffset->value() / MaxVisibleRows;

    if(page > 0)
      ui->rowOffset->setValue((page - 1) * MaxVisibleRows);
  });
  return b;
}

QPushButton *BufferViewer::MakeNextPageButton()
{
  QPushButton *b = new QPushButton(tr("Next Page"), this);
  QObject::connect(b, &QPushButton::clicked, [this] {
    int page = ui->rowOffset->value() / MaxVisibleRows;

    ui->rowOffset->setValue((page + 1) * MaxVisibleRows);
  });
  return b;
}

RDTableView *BufferViewer::tableForStage(MeshDataStage stage)
{
  if(stage == MeshDataStage::VSIn)
    return ui->inTable;
  else if(stage == MeshDataStage::VSOut)
    return ui->out1Table;
  else if(stage == MeshDataStage::GSOut)
    return ui->out2Table;
  else if(stage == MeshDataStage::TaskOut)
    return ui->out1Table;
  else if(stage == MeshDataStage::MeshOut)
    return ui->out2Table;

  return NULL;
}

BufferItemModel *BufferViewer::modelForStage(MeshDataStage stage)
{
  if(stage == MeshDataStage::VSIn)
    return m_ModelIn;
  else if(stage == MeshDataStage::VSOut)
    return m_ModelOut1;
  else if(stage == MeshDataStage::GSOut)
    return m_ModelOut2;
  else if(stage == MeshDataStage::TaskOut)
    return m_ModelOut1;
  else if(stage == MeshDataStage::MeshOut)
    return m_ModelOut2;

  return NULL;
}

bool BufferViewer::isCurrentRasterOut()
{
  BufferItemModel *model = currentBufferModel();

  // if geometry/tessellation is enabled, only the GS out stage is rasterized output
  if((m_Ctx.CurPipelineState().GetShader(ShaderStage::Tess_Eval) != ResourceId() ||
      m_Ctx.CurPipelineState().GetShader(ShaderStage::Geometry) != ResourceId()) &&
     m_CurStage != MeshDataStage::GSOut)
    return false;

  // task shader outputs are not rasterized by definition
  if(m_CurStage == MeshDataStage::TaskOut)
    return false;

  if(model)
  {
    int posEl = model->posColumn();
    if(posEl >= 0 && posEl < model->getConfig().columns.count())
    {
      return model->getConfig().props[posEl].systemValue == ShaderBuiltin::Position;
    }

    // if the model isn't prepared yet then return a sensible default answer - if no tess/geom,
    // vertex is the output. Otherwise geom is the output. For task/mesh then mesh is the output
    if(model->getConfig().columns.empty())
    {
      if(m_Ctx.CurPipelineState().GetShader(ShaderStage::Tess_Eval) != ResourceId() ||
         m_Ctx.CurPipelineState().GetShader(ShaderStage::Geometry) != ResourceId())
        return m_CurStage == MeshDataStage::GSOut;
      else if(m_CurStage == MeshDataStage::MeshOut)
        return true;
      else if(m_Ctx.CurPipelineState().GetShader(ShaderStage::Tess_Eval) == ResourceId() &&
              m_Ctx.CurPipelineState().GetShader(ShaderStage::Geometry) == ResourceId() &&
              m_CurStage == MeshDataStage::VSOut)
        return true;
    }
  }

  return false;
}

int BufferViewer::currentStageIndex()
{
  if(m_CurStage == MeshDataStage::VSIn)
    return 0;
  else if(m_CurStage == MeshDataStage::VSOut)
    return 1;
  else if(m_CurStage == MeshDataStage::GSOut)
    return 2;
  else if(m_CurStage == MeshDataStage::TaskOut)
    return 1;
  else if(m_CurStage == MeshDataStage::MeshOut)
    return 2;

  return 0;
}

bool BufferViewer::isMeshDraw()
{
  const ActionDescription *action = m_Ctx.CurAction();

  return action && action->flags & ActionFlags::MeshDispatch;
}

void BufferViewer::Reset()
{
  // Mesh preview work reads this pointer on the replay thread. Drain earlier
  // previews and detach it on that same queue before closing/switching capture.
  if(m_Output && m_Ctx.IsCaptureLoaded())
    m_Ctx.Replay().BlockInvoke([this](IReplayController *) { m_Output = NULL; });
  else
    m_Output = NULL;
  m_MeshCameraInitialised = false;

  configureDrawRange();

  ClearModels();

  updateLabelsAndLayout();
  SetMeshFilter(MeshFilter::None);

  ui->fixedVars->clear();

  ui->inTable->setColumnWidths({40, 40});
  ui->out1Table->setColumnWidths({40, 40});
  ui->out2Table->setColumnWidths({40, 40});

  m_BBoxes.clear();
}

void BufferViewer::ClearModels()
{
  for(BufferItemModel *m : {m_ModelIn, m_ModelOut1, m_ModelOut2})
  {
    if(!m)
      continue;

    m->beginReset();
    m->endReset(BufferConfiguration());
  }
}

void BufferViewer::CalcColumnWidth(int maxNumRows)
{
  // while the calculated column widths aren't actually isn't quite based on maxNumRows, it can only
  // be affected by a style change so that is good enough for us to cache it and save time
  // recalculating this repeatedly.
  if(m_ColumnWidthRowCount == maxNumRows)
    return;

  m_ColumnWidthRowCount = maxNumRows;

  ResourceFormat floatFmt;
  floatFmt.compByteWidth = 4;
  floatFmt.compType = CompType::Float;
  floatFmt.compCount = 1;

  ResourceFormat intFmt;
  intFmt.compByteWidth = 4;
  intFmt.compType = CompType::UInt;
  intFmt.compCount = 1;

  QString headerText = lit("ColumnSizeTest");

  BufferConfiguration bufconfig;

  BufferElementProperties floatProp, intProp;
  floatProp.format = floatFmt;
  intProp.format = intFmt;

  ShaderConstant elem;
  elem.name = headerText;
  elem.byteOffset = 0;
  elem.type.rows = maxNumRows;
  elem.type.columns = 1;

  bufconfig.columns.clear();

  bufconfig.columns.push_back(elem);
  bufconfig.props.push_back(floatProp);

  elem.type.rows = 1;
  elem.byteOffset = 4;

  bufconfig.columns.push_back(elem);
  bufconfig.props.push_back(floatProp);

  elem.byteOffset = 8;

  bufconfig.columns.push_back(elem);
  bufconfig.props.push_back(floatProp);

  elem.byteOffset = 12;

  bufconfig.columns.push_back(elem);
  bufconfig.props.push_back(intProp);

  elem.byteOffset = 16;

  bufconfig.columns.push_back(elem);
  bufconfig.props.push_back(intProp);

  bufconfig.numRows = 2;
  bufconfig.unclampedNumRows = 0;
  bufconfig.baseVertex = 0;

  if(bufconfig.indices)
    bufconfig.indices->deref();

  bufconfig.indices = new BufferData;
  bufconfig.indices->stride = sizeof(uint32_t);
  bufconfig.indices->storage.resize(sizeof(uint32_t) * 2);

  uint32_t *indices = (uint32_t *)bufconfig.indices->data();
  indices[0] = 0;
  indices[1] = 1000000;

  bufconfig.buffers.clear();

  struct TestData
  {
    float f[4];
    uint32_t ui[3];
  };

  BufferData *bufdata = new BufferData;
  bufdata->stride = sizeof(TestData);
  bufdata->storage.resize(sizeof(TestData));
  bufconfig.buffers.push_back(bufdata);

  TestData *test = (TestData *)bufdata->data();

  test->f[0] = 1.0f;
  test->f[1] = 1.2345e-20f;
  test->f[2] = 123456.7890123456789f;
  test->f[3] = -1.0f;

  test->ui[1] = 0x12345678;
  test->ui[2] = 0xffffffff;

  m_ModelIn->beginReset();

  m_ModelIn->endReset(bufconfig);

  // measure this data so we can use this as column widths
  ui->inTable->resizeColumnsToContents();

  // index/element column
  m_IdxColWidth = ui->inTable->columnWidth(0);

  int col = 1;
  if(m_MeshView)
    col = 2;

  m_DataColWidth = 10;
  for(int c = 0; c < 5; c++)
  {
    int colWidth = ui->inTable->columnWidth(col + c);
    m_DataColWidth = qMax(m_DataColWidth, colWidth);
  }

  ui->inTable->resizeRowsToContents();

  m_DataRowHeight = ui->inTable->rowHeight(0);
}

void BufferViewer::data_selected(const QItemSelection &selected, const QItemSelection &deselected)
{
  QObject *sender = QObject::sender();
  RDTableView *view = qobject_cast<RDTableView *>(sender);
  if(view == NULL)
    view = qobject_cast<RDTableView *>(sender->parent());

  if(view == NULL)
    return;

  if(m_UVSelectionUpdating)
    return;

  m_CurView = view;
  m_CurFixed = false;

  const bool vertexPair = !isMeshDraw() && (view == ui->inTable || view == ui->out1Table) &&
                          (currentTable() == ui->inTable || currentTable() == ui->out1Table);
  if(m_MeshView && m_UVData && (view == currentTable() || vertexPair) && m_UVData->geometry)
  {
    const QScopedValueRollback<bool> updating(m_UVSelectionUpdating, true);
    QSet<uint32_t> vertices;
    BufferItemModel *model = currentBufferModel();
    for(const QModelIndex &row : view->selectionModel()->selectedRows())
      if(uint32_t(row.row()) < model->getConfig().numRows)
        vertices.insert(model->meshIndex(uint32_t(row.row())));
    m_UVData->mode = MeshUVData::Selection::Point;
    m_UVCanvas->selectVertices(vertices);
    m_UVSelectedRows.clear();
    for(const QModelIndex &row : view->selectionModel()->selectedRows())
      m_UVSelectedRows.insert(row.row());
    m_UVSelectionStage = m_CurStage;
  }
  UpdateHighlightVerts();
  SyncViews(view, true, false);
  QueueMeshPreview();
}

void BufferViewer::data_scrolled(int scrollvalue)
{
  QObject *sender = QObject::sender();
  RDTableView *view = qobject_cast<RDTableView *>(sender);
  while(sender != NULL && view == NULL)
  {
    sender = sender->parent();
    view = qobject_cast<RDTableView *>(sender);
  }

  if(view == NULL)
    return;

  SyncViews(view, false, true);
}

void BufferViewer::UI_UpdateGuessParameters()
{
  m_Arcball->camera()->SetNearFar(m_Ctx.Config().MeshViewer_CameraNear,
                                  m_Ctx.Config().MeshViewer_CameraFar);
  m_Flycam->camera()->SetNearFar(m_Ctx.Config().MeshViewer_CameraNear,
                                 m_Ctx.Config().MeshViewer_CameraFar);

  m_Config.ortho = m_ProjGuess.orthographic;

  m_Config.fov = m_ProjGuess.fov;

  m_Config.aspect = 1.0f;

  // take a guess for the aspect ratio, for if the user hasn't overridden it
  Viewport vp = m_Ctx.CurPipelineState().GetViewport(0);

  float vpWidth = qAbs(vp.width);
  float vpHeight = qAbs(vp.height);

  m_Config.aspect = (vpWidth > 0.0f && vpHeight > 0.0f) ? (vpWidth / vpHeight) : 1.0f;

  if(m_ProjGuess.aspect > 0.0)
    m_Config.aspect = m_ProjGuess.aspect;

  // use estimates from post vs data (calculated from vertex position data) if the user
  // hasn't overridden the values
  m_Config.position.nearPlane = 0.1f;
  m_Config.position.flipY = false;

  if(m_CurStage == MeshDataStage::VSOut)
  {
    m_Config.position.nearPlane = m_Out1Data.nearPlane;
    m_Config.position.flipY = m_Out1Data.flipY;
  }
  else if(m_CurStage == MeshDataStage::GSOut)
  {
    m_Config.position.nearPlane = m_Out2Data.nearPlane;
    m_Config.position.flipY = m_Out2Data.flipY;
  }
  else if(m_CurStage == MeshDataStage::TaskOut)
  {
    m_Config.position.nearPlane = m_Out1Data.nearPlane;
    m_Config.position.flipY = m_Out1Data.flipY;
  }
  else if(m_CurStage == MeshDataStage::MeshOut)
  {
    m_Config.position.nearPlane = m_Out2Data.nearPlane;
    m_Config.position.flipY = m_Out2Data.flipY;
  }

  if(m_ProjGuess.nearPlane > 0.0)
    m_Config.position.nearPlane = m_ProjGuess.nearPlane;

  m_Config.position.farPlane = 100.0f;

  if(m_CurStage == MeshDataStage::VSOut)
    m_Config.position.farPlane = m_Out1Data.farPlane;
  else if(m_CurStage == MeshDataStage::GSOut)
    m_Config.position.farPlane = m_Out2Data.farPlane;
  else if(m_CurStage == MeshDataStage::TaskOut)
    m_Config.position.farPlane = m_Out1Data.farPlane;
  else if(m_CurStage == MeshDataStage::MeshOut)
    m_Config.position.farPlane = m_Out2Data.farPlane;

  if(m_ProjGuess.farPlane > 0.0)
    m_Config.position.farPlane = m_ProjGuess.farPlane;

  UpdateStageDataControls();

  INVOKE_MEMFN(RT_UpdateAndDisplay);
}

void BufferViewer::on_axisMappingCombo_currentIndexChanged(int index)
{
  const MeshDisplay previous = m_Config;
  if(index != 4)
  {
    switch(index)
    {
      case 0:    // Y-up, Left Handed
        m_Config.axisMapping.xAxis = FloatVector(1.0f, 0.0f, 0.0f, 0.0f);
        m_Config.axisMapping.yAxis = FloatVector(0.0f, 1.0f, 0.0f, 0.0f);
        m_Config.axisMapping.zAxis = FloatVector(0.0f, 0.0f, 1.0f, 0.0f);
        break;
      case 1:    // Y-up, Right Handed
        m_Config.axisMapping.xAxis = FloatVector(1.0f, 0.0f, 0.0f, 0.0f);
        m_Config.axisMapping.yAxis = FloatVector(0.0f, 1.0f, 0.0f, 0.0f);
        m_Config.axisMapping.zAxis = FloatVector(0.0f, 0.0f, -1.0f, 0.0f);
        break;
      case 2:    // Z-up, Left Handed
        m_Config.axisMapping.xAxis = FloatVector(1.0f, 0.0f, 0.0f, 0.0f);
        m_Config.axisMapping.yAxis = FloatVector(0.0f, 0.0f, -1.0f, 0.0f);
        m_Config.axisMapping.zAxis = FloatVector(0.0f, 1.0f, 0.0f, 0.0f);
        break;
      case 3:    // Z-up, Right Handed
        m_Config.axisMapping.xAxis = FloatVector(1.0f, 0.0f, 0.0f, 0.0f);
        m_Config.axisMapping.yAxis = FloatVector(0.0f, 0.0f, 1.0f, 0.0f);
        m_Config.axisMapping.zAxis = FloatVector(0.0f, 1.0f, 0.0f, 0.0f);
        break;
      default: break;
    }
    ui->axisMappingButton->setEnabled(false);
    previousAxisMappingIndex = index;
    RemapCameraPivots(previous);
    QueueMeshPreview();
  }
  else
  {
    ui->axisMappingButton->setEnabled(true);
    if(previousAxisMappingIndex != 4)
    {
      bool validConfig = showAxisMappingDialog();

      if(!validConfig)
      {
        ui->axisMappingCombo->setCurrentIndex(previousAxisMappingIndex);
        ui->axisMappingButton->setEnabled(false);
      }
    }
  }
}

bool BufferViewer::showAxisMappingDialog()
{
  AxisMappingDialog dialog(m_Ctx, m_Config, this);
  RDDialog::show(&dialog);

  if(dialog.result() == QDialog::Accepted)
  {
    const MeshDisplay previous = m_Config;
    m_Config.axisMapping = dialog.getAxisMapping();
    RemapCameraPivots(previous);
    QueueMeshPreview();
    return true;
  }
  return false;
}

void BufferViewer::RemapCameraPivots(const MeshDisplay &previous)
{
  if(!m_Arcball || !m_Flycam)
    return;
  float before[16], after[16];
  RENDERDOC_GetMeshPreviewModelTransform(previous, m_Ctx.APIProps().pipelineType, before);
  RENDERDOC_GetMeshPreviewModelTransform(m_Config, m_Ctx.APIProps().pipelineType, after);
  for(int i = 0; i < 16; ++i)
    if(!std::isfinite(before[i]) || !std::isfinite(after[i]))
      return;
  bool invertible = false;
  const QMatrix4x4 inverse = QMatrix4x4(before).transposed().inverted(&invertible);
  if(!invertible)
    return;
  // Axis mapping transforms model coordinates, never the mouse's camera axes.
  // Carry the same model-space focus into the new mapping without resetting orbit or zoom.
  const QMatrix4x4 remap = QMatrix4x4(after).transposed() * inverse;
  for(int i = 0; i < 16; ++i)
    if(!std::isfinite(remap.constData()[i]))
      return;
  m_Arcball->RemapPivot(remap);
  m_Flycam->RemapPivot(remap);
}

void BufferViewer::on_axisMappingButton_clicked()
{
  showAxisMappingDialog();
}

void BufferViewer::on_camParameters_clicked()
{
  CameraControlsDialog dialog(m_Ctx, this);
  RDDialog::show(&dialog);

  if(dialog.result() == QDialog::Accepted)
  {
    if(m_CameraPreset)
      m_CameraPreset->setCurrentIndex(qBound(0, m_Ctx.Config().MeshViewer_CameraNavigation, 2));
    if(m_Ctx.Config().MeshViewer_CameraNavigation != 0)
      ui->controlType->setCurrentIndex(0);
    UI_UpdateGuessParameters();
    QueueMeshPreview();
  }
}

void BufferViewer::on_guessButton_clicked()
{
  ProjectionGuessDialog dialog(m_Ctx, m_ProjGuess, this);
  RDDialog::show(&dialog);

  if(dialog.result() == QDialog::Accepted)
  {
    m_ProjGuess = dialog.getParameters();
    UI_UpdateGuessParameters();
  }
}

void BufferViewer::on_setFormat_toggled(bool checked)
{
  if(!checked)
  {
    ui->formatSpecifier->setVisible(false);

    processFormat(QString());
    return;
  }

  ui->formatSpecifier->setVisible(true);

  const ShaderReflection *reflection =
      CBufferReflection();

  if(m_CBufferSlot.slot >= reflection->constantBlocks.size())
  {
    ui->formatSpecifier->setVisible(false);

    processFormat(QString());
    return;
  }

  if(IsD3D(m_Ctx.APIProps().pipelineType))
    ui->formatSpecifier->setAutoFormat(BufferFormatter::DeclareStruct(
        PackingRules::D3DCB(), reflection->resourceId,
        reflection->constantBlocks[m_CBufferSlot.slot].name,
        reflection->constantBlocks[m_CBufferSlot.slot].variables, 0));
  else
    ui->formatSpecifier->setAutoFormat(BufferFormatter::DeclareStruct(
        BufferFormatter::EstimatePackingRules(
            reflection->resourceId, reflection->constantBlocks[m_CBufferSlot.slot].variables),
        reflection->resourceId, reflection->constantBlocks[m_CBufferSlot.slot].name,
        reflection->constantBlocks[m_CBufferSlot.slot].variables, 0));
}

void BufferViewer::on_resetMeshFilterButton_clicked()
{
  SetMeshFilter(MeshFilter::None);
}

void BufferViewer::processFormat(const QString &format)
{
  // save scroll values now before we reset all the models
  m_Scrolls = new PopulateBufferData;
  FillScrolls(m_Scrolls);

  Reset();

  BufferConfiguration bufconfig;

  ParsedFormat parsed;

  if(IsCBufferView() && format.isEmpty())
  {
    // insert a dummy member so we get identified as plain fixed vars - we will automatically
    // evaluate ignoring the format
    parsed.fixed.type.members.push_back(ShaderConstant());
  }
  else
  {
    parsed = BufferFormatter::ParseFormatString(format, m_ByteSize, IsCBufferView());
  }

  const bool repeatedVars = parsed.repeating.type.baseType != VarType::Unknown;
  const bool fixedVars = !parsed.fixed.type.members.empty();

  if(fixedVars && repeatedVars)
  {
    if(m_OuterSplitter->widget(0) != m_InnerSplitter)
      m_OuterSplitter->replaceWidget(0, m_InnerSplitter);

    m_FixedGroup->layout()->addWidget(ui->fixedVars);
    m_RepeatedGroup->layout()->addWidget(ui->inTable);

    // row offset should be shown in the repeated control bar, but no separator line is needed
    ui->offsetLine->setVisible(false);
    ui->rowOffsetLabel->setVisible(true);
    ui->rowOffset->setVisible(true);
    if(ui->rowOffset->parentWidget() != m_RepeatedControlBar)
    {
      QHBoxLayout *hbox = qobject_cast<QHBoxLayout *>(m_RepeatedControlBar->layout());
      hbox->insertWidget(0, ui->rowOffsetLabel);
      hbox->insertWidget(1, ui->rowOffset);
    }
    ui->fixedVars->setVisible(true);
    ui->inTable->setVisible(true);

    ui->showPadding->setVisible(true);

    m_InnerSplitter->setVisible(true);

    if(m_CurView == NULL && !m_CurFixed)
      m_CurView = ui->inTable;
  }
  else if(fixedVars)
  {
    if(m_OuterSplitter->widget(0) != ui->fixedVars)
      m_OuterSplitter->replaceWidget(0, ui->fixedVars);

    // row offset should not be shown
    ui->offsetLine->setVisible(false);
    ui->rowOffsetLabel->setVisible(false);
    ui->rowOffset->setVisible(false);

    ui->fixedVars->setVisible(true);
    ui->inTable->setVisible(false);

    ui->showPadding->setVisible(true);

    m_InnerSplitter->setVisible(false);

    m_CurView = NULL;
    m_CurFixed = true;
  }
  else if(repeatedVars)
  {
    if(m_OuterSplitter->widget(0) != ui->inTable)
      m_OuterSplitter->replaceWidget(0, ui->inTable);

    // row offset should be shown with the other controls
    ui->offsetLine->setVisible(true);
    ui->rowOffsetLabel->setVisible(true);
    ui->rowOffset->setVisible(true);
    // insert after the offsetLine
    if(ui->rowOffset->parentWidget() != ui->meshToolbar)
    {
      QHBoxLayout *hbox = qobject_cast<QHBoxLayout *>(ui->meshToolbar->layout());

      int i = 0;
      for(; i < hbox->count(); i++)
      {
        if(hbox->itemAt(i)->widget() == ui->offsetLine)
          break;
      }
      i++;
      if(i < hbox->count())
      {
        hbox->insertWidget(i, ui->rowOffset);
        hbox->insertWidget(i, ui->rowOffsetLabel);
      }
    }

    ui->fixedVars->setVisible(false);
    ui->inTable->setVisible(true);

    ui->showPadding->setVisible(false);

    m_InnerSplitter->setVisible(false);

    m_CurView = ui->inTable;
    m_CurFixed = false;
  }

  CalcColumnWidth(MaxNumRows(parsed.repeating));

  ClearModels();

  m_Format = format;

  if(IsCBufferView())
  {
    ui->byteRangeLine->setVisible(false);
    ui->byteRangeStartLabel->setVisible(false);
    byteRangeStart->setVisible(false);
    ui->byteRangeLengthLabel->setVisible(false);
    byteRangeLength->setVisible(false);
    GraphicsAPI pipeType = m_Ctx.APIProps().pipelineType;

    if(IsD3D(pipeType))
      ui->formatSpecifier->setTitle(tr("Constant Buffer Custom Format"));
    else
      ui->formatSpecifier->setTitle(tr("Uniform Buffer Custom Format"));
  }
  else
  {
    qulonglong stride = qMax(1U, parsed.repeating.type.arrayByteStride);

    byteRangeStart->setSingleStep(stride);
    byteRangeLength->setSingleStep(stride);

    byteRangeStart->setMaximum((qulonglong)m_ObjectByteSize);
    byteRangeLength->setMaximum((qulonglong)m_ObjectByteSize);

    byteRangeStart->setValue(m_ByteOffset);
    byteRangeLength->setValue(m_ByteSize);

    if(!m_IsBuffer)
    {
      byteRangeStart->setVisible(false);

      TextureDescription *tex = m_Ctx.GetTexture(m_BufferID);
      if(tex)
      {
        if(tex->arraysize == 1 && tex->mips == 1 && tex->msSamp == 1 && tex->depth == 1)
        {
          ui->byteRangeStartLabel->setVisible(false);
        }
        else
        {
          QString text;

          if(tex->arraysize > 1 || tex->depth > 1)
            text = tr("Slice %1").arg(m_TexSub.slice);

          if(tex->mips > 1)
          {
            if(!text.isEmpty())
              text += lit(", ");
            text += tr("Mip %1").arg(m_TexSub.mip);
          }

          if(tex->msSamp > 1)
          {
            if(!text.isEmpty())
              text += lit(", ");
            text += tr("Sample %1").arg(m_TexSub.sample);
          }

          text += lit(". ");

          ui->byteRangeStartLabel->setText(text);
        }
      }
      byteRangeLength->setEnabled(false);
    }
  }

  ui->formatSpecifier->setErrors(parsed.errors);

  OnEventChanged(CBufferEvent());
}

void BufferViewer::on_byteRangeStart_valueChanged(double value)
{
  m_ByteOffset = RDSpinBox64::getUValue(value);

  m_PagingByteOffset = 0;

  processFormat(m_Format);
}

void BufferViewer::on_byteRangeLength_valueChanged(double value)
{
  m_ByteSize = RDSpinBox64::getUValue(value);

  m_PagingByteOffset = 0;

  processFormat(m_Format);
}

void BufferViewer::updateExportActionNames()
{
  QString csv = tr("Export%1 to &CSV");
  QString bytes = tr("Export%1 to &Bytes");

  bool valid = m_Ctx.IsCaptureLoaded() && m_Ctx.CurAction();

  if(m_MeshView)
  {
    valid = valid && m_CurView != NULL;
  }
  else
  {
    valid = valid && (m_CurView != NULL || m_CurFixed);
  }

  if(!valid)
  {
    m_ExportCSV->setText(csv.arg(QString()));
    m_ExportBytes->setText(bytes.arg(QString()));
    m_ExportCSV->setEnabled(false);
    m_ExportBytes->setEnabled(false);
    return;
  }

  m_ExportCSV->setEnabled(true);
  m_ExportBytes->setEnabled(m_BufferID != ResourceId());

  if(m_MeshView)
  {
    m_ExportCSV->setText(csv.arg(lit(" ") + m_CurView->windowTitle()));
    m_ExportBytes->setText(bytes.arg(lit(" ") + m_CurView->windowTitle()));
    m_ExportBytes->setEnabled(true);
  }
  else
  {
    // if only one type of data is visible, the export is unambiguous
    if(!ui->inTable->isVisible() || !ui->fixedVars->isVisible())
    {
      m_ExportCSV->setText(csv.arg(QString()));
      m_ExportBytes->setText(bytes.arg(QString()));
    }
    // otherwise go by which is selected
    else if(m_CurFixed)
    {
      m_ExportCSV->setText(csv.arg(lit(" ") + m_FixedGroup->title()));
      m_ExportBytes->setText(bytes.arg(lit(" ") + m_FixedGroup->title()));
    }
    else
    {
      m_ExportCSV->setText(csv.arg(lit(" ") + m_RepeatedGroup->title()));
      m_ExportBytes->setText(bytes.arg(lit(" ") + m_RepeatedGroup->title()));
    }
  }
}

void BufferViewer::exportCSV(QTextStream &ts, const QString &prefix, RDTreeWidgetItem *item)
{
  for(int col = 0; col < ui->fixedVars->header()->count(); col++)
  {
    if(col > 0)
      ts << QLatin1Char(',');
    QString text = col < item->dataCount() ? item->text(col) : QString();
    text.replace(QLatin1Char('"'), QStringLiteral("\"\""));
    ts << QLatin1Char('"') << text << QLatin1Char('"');
  }
  ts << QLatin1Char('\n');
  for(int i = 0; i < item->childCount(); i++)
    exportCSV(ts, prefix, item->child(i));
}

void BufferViewer::exportData(const BufferExport &params)
{
  if(!m_Ctx.IsCaptureLoaded())
    return;

  if(!m_Ctx.CurAction() && !m_CBufferPinnedReflection)
    return;

  if(!m_CurView && !m_CurFixed)
    return;

  QString filter;
  QString title;
  if(params.format == BufferExport::CSV)
  {
    filter = tr("CSV Files (*.csv)");
    title = tr("Export buffer to CSV");
  }
  else if(params.format == BufferExport::RawBytes)
  {
    filter = tr("Binary Files (*.bin)");
    title = tr("Export buffer to bytes");
  }

  QString filename =
      RDDialog::getSaveFileName(this, title, QString(), tr("%1;;All files (*)").arg(filter));

  if(filename.isEmpty())
    return;

  QFile *f = new QFile(filename);

  QIODevice::OpenMode flags = QIODevice::WriteOnly | QFile::Truncate;

  if(params.format == BufferExport::CSV)
    flags |= QIODevice::Text;

  if(!f->open(flags))
  {
    delete f;
    RDDialog::critical(this, tr("Error exporting file"),
                       tr("Couldn't open file '%1' for writing").arg(filename));
    return;
  }

  if(m_MeshView)
  {
    ANALYTIC_SET(Export.MeshOutput, true);
  }
  else
  {
    ANALYTIC_SET(Export.RawBuffer, true);
  }

  if(m_CurView)
  {
    BufferItemModel *model = (BufferItemModel *)m_CurView->model();
    const uint32_t pinnedEvent = m_CBufferPinnedReflection ? m_CBufferPinnedEvent : 0;

    LambdaThread *exportThread = new LambdaThread([this, params, model, f, pinnedEvent]() {
      if(params.format == BufferExport::RawBytes)
      {
        const BufferConfiguration &config = model->getConfig();

        if(!m_MeshView)
        {
          // this is the simplest possible case, we just dump the contents of the first buffer.
          if(!m_IsBuffer || config.buffers[0]->size() >= m_ByteSize)
          {
            f->write((const char *)config.buffers[0]->data(), int(config.buffers[0]->size()));
          }
          else
          {
            // For buffers we have to handle reading in pages though as we might not have everything
            // in memory.
            ResourceId buff = m_BufferID;

            static const uint64_t maxChunkSize = 4 * 1024 * 1024;
            for(uint64_t byteOffset = m_ByteOffset; byteOffset < m_ByteSize + m_ByteOffset;
                byteOffset += maxChunkSize)
            {
              uint64_t chunkSize = qMin(m_ByteOffset + m_ByteSize - byteOffset, maxChunkSize);

              // it's fine to block invoke, because this is on the export thread
              m_Ctx.Replay().BlockInvoke([buff, f, byteOffset, chunkSize, pinnedEvent](IReplayController *r) {
                CBufferReplayEventScope replayEvent(r, pinnedEvent);
                bytebuf chunk = r->GetBufferData(buff, byteOffset, chunkSize);
                f->write((const char *)chunk.data(), (qint64)chunk.size());
              });
            }
          }
        }
        else
        {
          // cache column data for the inner loop
          QVector<CachedElData> cache;

          CacheDataForIteration(cache, config.columns, config.props, config.buffers,
                                config.curInstance);

          // go row by row, finding the start of the row and dumping out the elements using their
          // offset and sizes
          for(int i = 0; i < model->rowCount(); i++)
          {
            // manually calculate the index so that we get the real offset (not the displayed
            // offset)
            // in the case of vertex output.
            uint32_t idx = i;

            if(config.indices && config.indices->hasData())
            {
              idx = CalcIndex(config.indices, i, config.baseVertex, config.primRestart);

              // completely omit primitive restart indices
              if(config.primRestart && idx == config.primRestart)
                continue;
            }

            for(int col = 0; col < cache.count(); col++)
            {
              const CachedElData &d = cache[col];
              const ShaderConstant *el = d.el;
              const BufferElementProperties *prop = d.prop;

              if(d.data)
              {
                const char *bytes = (const char *)d.data;

                if(!prop->perinstance)
                  bytes += d.stride * idx;

                if(bytes + d.byteSize <= (const char *)d.end)
                {
                  f->write(bytes, d.byteSize);
                  continue;
                }
              }

              // if we didn't continue above, something was wrong, so write nulls
              f->write(d.nulls);
            }
          }
        }
      }
      else if(params.format == BufferExport::CSV)
      {
        // otherwise we need to iterate over all the data ourselves
        const BufferConfiguration &config = model->getConfig();

        QTextStream s(f);

        for(int i = 0; i < model->columnCount(); i++)
        {
          s << model->headerData(i, Qt::Horizontal, Qt::DisplayRole).toString();

          if(i + 1 < model->columnCount())
            s << ", ";
        }

        s << "\n";

        if(m_MeshView || !m_IsBuffer || config.buffers[0]->size() >= m_ByteSize)
        {
          // if there's no pagination to worry about, dump using the model's data()
          for(int row = 0; row < model->rowCount(); row++)
          {
            for(int col = 0; col < model->columnCount(); col++)
            {
              QList<QString> lines =
                  model->data(model->index(row, col), Qt::DisplayRole).toString().split(lit("\n"));
              bool quote = (lines.count() > 1);
              if(quote)
                s << "\"";
              for(int l = 0; l < lines.count(); l++)
              {
                s << lines[l].trimmed();
                if(l + 1 < lines.size())
                  s << "\n";
              }
              if(quote)
                s << "\"";

              if(col + 1 < model->columnCount())
                s << ", ";
            }

            s << "\n";
          }
        }
        else
        {
          // write 64k rows at a time
          ResourceId buff = m_BufferID;
          const uint64_t maxChunkSize = 64 * 1024 * config.buffers[0]->stride;
          for(uint64_t byteOffset = m_ByteOffset; byteOffset < m_ByteSize; byteOffset += maxChunkSize)
          {
            uint64_t chunkSize = qMin(m_ByteSize - byteOffset, maxChunkSize);

            // it's fine to block invoke, because this is on the export thread
            m_Ctx.Replay().BlockInvoke(
                [buff, &s, &config, byteOffset, chunkSize, pinnedEvent](IReplayController *controller) {
                  CBufferReplayEventScope replayEvent(controller, pinnedEvent);
                  // cache column data for the inner loop
                  QVector<CachedElData> cache;

                  BufferData bufferData;

                  bufferData.storage = controller->GetBufferData(buff, byteOffset, chunkSize);
                  bufferData.stride = config.buffers[0]->stride;

                  size_t numRows =
                      (bufferData.storage.size() + bufferData.stride - 1) / bufferData.stride;
                  size_t rowOffset = byteOffset / bufferData.stride;

                  CacheDataForIteration(cache, config.columns, config.props, {&bufferData}, 0);

                  // go row by row, finding the start of the row and dumping out the elements using
                  // their
                  // offset and sizes
                  for(size_t idx = 0; idx < numRows; idx++)
                  {
                    s << (rowOffset + idx) << ", ";

                    for(int col = 0; col < cache.count(); col++)
                    {
                      const CachedElData &d = cache[col];
                      const ShaderConstant *el = d.el;
                      const BufferElementProperties *prop = d.prop;

                      if(d.data)
                      {
                        const byte *data = d.data;
                        const byte *end = d.end;

                        data += d.stride * idx;

                        // only slightly wasteful, we need to fetch all variants together
                        // since some formats are packed and can't be read individually
                        QVariantList list = GetVariants(prop->format, *el, data, end);

                        if(el->type.rows > 1)
                        {
                          for(int c = 0; c < el->type.columns; c++)
                          {
                            s << "\"";
                            for(int r = 0; r < el->type.rows; r++)
                            {
                              if(list.empty())
                              {
                                s << "---";
                              }
                              else
                              {
                                int el_idx = r * el->type.columns + c;
                                s << interpretVariant(list[el_idx], *el, *prop).trimmed();
                              }

                              if(r + 1 < el->type.rows)
                                s << "\n";
                            }
                            s << "\", ";
                          }
                        }
                        else if(list.empty())
                        {
                          for(int v = 0; v < d.numColumns; v++)
                          {
                            s << "---";

                            if(v + 1 < d.numColumns)
                              s << ", ";
                          }
                        }
                        else
                        {
                          for(int v = 0; v < list.count(); v++)
                          {
                            s << interpretVariant(list[v], *el, *prop);

                            if(v + 1 < list.count())
                              s << ", ";
                          }
                        }

                        if(col + 1 < cache.count())
                          s << ", ";
                      }
                    }

                    s << "\n";
                  }
                });
          }
        }
      }

      f->close();

      delete f;
    });
    exportThread->start();

    ShowProgressDialog(this, tr("Exporting data"),
                       [exportThread]() { return !exportThread->isRunning(); });

    exportThread->deleteLater();
  }
  else if(m_CurFixed)
  {
    if(params.format == BufferExport::RawBytes)
    {
      BufferItemModel *model = (BufferItemModel *)ui->inTable->model();
      const BufferConfiguration &config = model->getConfig();

      size_t byteSize = 0;

      if(!config.fixedVars.type.members.empty())
        byteSize = BufferFormatter::GetVarAdvance(config.packing, config.fixedVars);

      const bytebuf &bufdata = config.buffers[0]->storage;

      f->write((const char *)bufdata.data(), qMin(bufdata.size(), byteSize));

      // if the buffer wasn't large enough for the variables, fill with 0s
      if(byteSize > bufdata.size())
      {
        QByteArray nulls;
        nulls.resize(int(byteSize - config.buffers[0]->storage.size()));
        f->write(nulls);
      }
    }
    else if(params.format == BufferExport::CSV)
    {
      QTextStream ts(f);

      for(int col = 0; col < ui->fixedVars->header()->count(); col++)
      {
        if(col > 0)
          ts << QLatin1Char(',');
        QString text = ui->fixedVars->headerText(col);
        text.replace(QLatin1Char('"'), QStringLiteral("\"\""));
        ts << QLatin1Char('"') << text << QLatin1Char('"');
      }
      ts << QLatin1Char('\n');

      for(int i = 0; i < ui->fixedVars->topLevelItemCount(); i++)
        exportCSV(ts, QString(), ui->fixedVars->topLevelItem(i));
    }

    f->close();

    delete f;
  }
}

void BufferViewer::debugVertex()
{
  if(!m_Ctx.IsCaptureLoaded())
    return;

  if(!m_Ctx.CurAction())
    return;

  if(!m_CurView)
    return;

  QModelIndex idx = m_CurView->selectionModel()->currentIndex();

  if(!idx.isValid())
  {
    GUIInvoke::call(this, [this]() {
      RDDialog::critical(this, tr("Error debugging"),
                         tr("Error debugging vertex - make sure a valid vertex is selected"));
    });
    return;
  }

  uint32_t vertid =
      m_CurView->model()->data(m_CurView->model()->index(idx.row(), 0), Qt::DisplayRole).toUInt();
  uint32_t index =
      m_CurView->model()->data(m_CurView->model()->index(idx.row(), 1), Qt::DisplayRole).toUInt();
  uint32_t view = m_Config.curView;

  bool done = false;
  ShaderDebugTrace *trace = NULL;

  m_Ctx.Replay().AsyncInvoke([this, &done, &trace, vertid, index, view](IReplayController *r) {
    trace = r->DebugVertex(vertid, m_Config.curInstance, index, view);

    if(trace->debugger == NULL)
    {
      r->FreeTrace(trace);
      trace = NULL;
    }

    done = true;
  });

  QString debugContext = tr("Vertex %1").arg(vertid);

  if(m_Ctx.CurAction()->numInstances > 1)
    debugContext += tr(", Instance %1").arg(m_Config.curInstance);

  // wait a short while before displaying the progress dialog (which won't show if we're already
  // done by the time we reach it)
  for(int i = 0; !done && i < 100; i++)
    QThread::msleep(5);

  ShowProgressDialog(this, tr("Debugging %1").arg(debugContext), [&done]() { return done; });

  if(!trace)
  {
    RDDialog::critical(this, tr("Error debugging"),
                       tr("Error debugging vertex - make sure a valid vertex is selected"));
    return;
  }

  const ShaderReflection *shaderDetails =
      m_Ctx.CurPipelineState().GetShaderReflection(ShaderStage::Vertex);
  ResourceId pipeline = m_Ctx.CurPipelineState().GetGraphicsPipelineObject();

  // viewer takes ownership of the trace
  IShaderViewer *s = m_Ctx.DebugShader(shaderDetails, pipeline, trace, debugContext);

  m_Ctx.AddDockWindow(s->Widget(), DockReference::AddTo, this);
}

void BufferViewer::debugMeshThread()
{
  if(!m_Ctx.IsCaptureLoaded())
    return;

  const ActionDescription *action = m_Ctx.CurAction();
  if(!action)
    return;

  if(!m_CurView)
    return;

  QModelIndex idx = m_CurView->selectionModel()->currentIndex();

  if(!idx.isValid())
  {
    GUIInvoke::call(this, [this]() {
      RDDialog::critical(this, tr("Error debugging"),
                         tr("Error debugging meshlet - make sure a valid meshlet is selected"));
    });
    return;
  }

  uint32_t taskIndex = 0, meshletIndex = 0;
  GetIndicesForMeshRow((uint32_t)idx.row(), taskIndex, meshletIndex);

  const ShaderReflection *shaderDetails =
      m_Ctx.CurPipelineState().GetShaderReflection(ShaderStage::Mesh);

  if(!shaderDetails)
    return;

  rdcfixedarray<uint32_t, 3> threadGroupSize = action->dispatchThreadsDimension[0] == 0
                                                   ? shaderDetails->dispatchThreadsDimension
                                                   : action->dispatchThreadsDimension;
  m_MeshDebugSelector->SetThreadBounds(action->dispatchDimension, threadGroupSize);

  // Calculate 3d group id from 1d meshlet index and dispatch dimensions
  // Imagine 8x2x4 with idx 60
  // 8x2 = 16
  // 8x2x4 = 64
  // 60 % x = 4
  // 60 % (x * y) = 12 / x = 1
  // 60 / (x * y) = 3
  // index 60 is id (4,1,3)
  // 4 + (8 * 1) + (16 * 3) = 60
  uint32_t xDim = action->dispatchDimension[0];
  uint32_t yDim = action->dispatchDimension[1];
  uint32_t zDim = action->dispatchDimension[2];
  rdcfixedarray<uint32_t, 3> meshletGroup = {
      meshletIndex % xDim,
      (meshletIndex % (xDim * yDim)) / xDim,
      meshletIndex / (xDim * yDim),
  };
  m_MeshDebugSelector->SetDefaultDispatch(meshletGroup, {0, 0, 0});

  RDDialog::show(m_MeshDebugSelector);
}

void BufferViewer::meshDebugSelector_beginDebug(const rdcfixedarray<uint32_t, 3> &group,
                                                const rdcfixedarray<uint32_t, 3> &thread)
{
  const ActionDescription *action = m_Ctx.CurAction();

  if(!action)
    return;

  const ShaderReflection *shaderDetails =
      m_Ctx.CurPipelineState().GetShaderReflection(ShaderStage::Mesh);

  if(!shaderDetails)
    return;

  // The progress dialog can publish a newer event while this debug request is running.
  // Retain the reflection used to start the request until the viewer has taken its own copy.
  const ShaderReflection shaderSnapshot(*shaderDetails);
  shaderDetails = &shaderSnapshot;
  const ResourceId pipeline = m_Ctx.CurPipelineState().GetComputePipelineObject();

  struct threadSelect
  {
    rdcfixedarray<uint32_t, 3> g;
    rdcfixedarray<uint32_t, 3> t;
  } debugThread = {
      // g[]
      {group[0], group[1], group[2]},
      // t[]
      {thread[0], thread[1], thread[2]},
  };

  bool done = false;
  ShaderDebugTrace *trace = NULL;

  m_Ctx.Replay().AsyncInvoke([&trace, &done, debugThread](IReplayController *r) {
    trace = r->DebugMeshThread(debugThread.g, debugThread.t);

    if(trace->debugger == NULL)
    {
      r->FreeTrace(trace);
      trace = NULL;
    }

    done = true;
  });

  QString debugContext = lit("Mesh Group [%1,%2,%3] Thread [%4,%5,%6]")
                             .arg(group[0])
                             .arg(group[1])
                             .arg(group[2])
                             .arg(thread[0])
                             .arg(thread[1])
                             .arg(thread[2]);

  // wait a short while before displaying the progress dialog (which won't show if we're already
  // done by the time we reach it)
  for(int i = 0; !done && i < 100; i++)
    QThread::msleep(5);

  ShowProgressDialog(this, tr("Debugging %1").arg(debugContext), [&done]() { return done; });

  if(!trace)
  {
    RDDialog::critical(
        this, tr("Error debugging"),
        tr("Error debugging thread - make sure a valid group and thread is selected"));
    return;
  }

  // viewer takes ownership of the trace
  IShaderViewer *s = m_Ctx.DebugShader(
      shaderDetails, pipeline, trace, debugContext);

  m_Ctx.AddDockWindow(s->Widget(), DockReference::AddTo, this);
}

void BufferViewer::SyncViews(RDTableView *primary, bool selection, bool scroll)
{
  if(!ui->syncViews->isChecked())
    return;

  RDTableView *views[] = {ui->inTable, ui->out1Table, ui->out2Table};

  int horizScrolls[ARRAY_COUNT(views)] = {0};

  for(size_t i = 0; i < ARRAY_COUNT(views); i++)
    horizScrolls[i] = views[i]->horizontalScrollBar()->value();

  if(primary == NULL)
  {
    for(RDTableView *table : views)
    {
      if(table->hasFocus())
      {
        primary = table;
        break;
      }
    }
  }

  if(primary == NULL)
    primary = views[0];

  for(RDTableView *table : views)
  {
    if(table == primary)
      continue;

    if(selection)
    {
      const bool vertexPair = (primary == ui->inTable && table == ui->out1Table) ||
                              (primary == ui->out1Table && table == ui->inTable);
      if(vertexPair && !isMeshDraw())
      {
        QItemSelection rows;
        const int columns = table->model()->columnCount();
        for(const auto &range : primary->selectionModel()->selection())
          if(range.top() < table->model()->rowCount() && columns > 0)
            rows.select(table->model()->index(range.top(), 0),
                        table->model()->index(qMin(range.bottom(), table->model()->rowCount() - 1),
                                              columns - 1));
        const QSignalBlocker block(table->selectionModel());
        table->selectionModel()->select(
            rows, QItemSelectionModel::ClearAndSelect | QItemSelectionModel::Rows);
      }
    }

    if(scroll)
      table->verticalScrollBar()->setValue(primary->verticalScrollBar()->value());
  }

  for(size_t i = 0; i < ARRAY_COUNT(views); i++)
    views[i]->horizontalScrollBar()->setValue(horizScrolls[i]);
}

void BufferViewer::UpdateHighlightVerts()
{
  m_Config.highlightVert = ~0U;

  if(ui->highlightVerts->isHidden() || !ui->highlightVerts->isChecked())
    return;

  RDTableView *table = currentTable();

  if(!table)
    return;

  QModelIndexList selected = table->selectionModel()->selectedRows();

  if(selected.empty())
    return;

  m_Config.highlightVert = selected[0].row();
}

void BufferViewer::UpdateStageDataControls()
{
  if(isCurrentRasterOut())
  {
    ui->guessLabel->setVisible(true);
    ui->guessDetails1->setVisible(true);
    ui->guessDetails2->setVisible(true);
    ui->guessButton->setVisible(true);

    QString aspectStr = tr("Auto");
    if(m_ProjGuess.aspect > 0)
      aspectStr = Formatter::Format(m_ProjGuess.aspect);

    if(m_ProjGuess.orthographic)
      ui->guessDetails1->setText(tr("Orthographic Projection"));
    else
      ui->guessDetails1->setText(
          tr("Perspective Projection, FOV %1").arg(Formatter::Format(m_ProjGuess.fov)));

    if(m_ProjGuess.farPlane == FLT_MAX)
    {
      if(m_ProjGuess.nearPlane > 0)
        ui->guessDetails2->setText(tr("Aspect Ratio %1, Reverse Z Near %2")
                                       .arg(aspectStr)
                                       .arg(Formatter::Format(m_ProjGuess.nearPlane)));
      else
        ui->guessDetails2->setText(tr("Aspect Ratio %1, Reverse Z Near Automatic").arg(aspectStr));
    }
    else
    {
      if(m_ProjGuess.nearPlane > 0 && m_ProjGuess.farPlane > 0)
        ui->guessDetails2->setText(tr("Aspect Ratio %1, Near-Far %2 - %3")
                                       .arg(aspectStr)
                                       .arg(Formatter::Format(m_ProjGuess.nearPlane))
                                       .arg(Formatter::Format(m_ProjGuess.farPlane)));
      else if(m_ProjGuess.nearPlane > 0)
        ui->guessDetails2->setText(tr("Aspect Ratio %1, Near %2 Far Auto")
                                       .arg(aspectStr)
                                       .arg(Formatter::Format(m_ProjGuess.nearPlane)));
      else if(m_ProjGuess.farPlane > 0)
        ui->guessDetails2->setText(tr("Aspect Ratio %1, Near Auto Far %2")
                                       .arg(aspectStr)
                                       .arg(Formatter::Format(m_ProjGuess.farPlane)));
      else
        ui->guessDetails2->setText(tr("Aspect Ratio %1, Near-Far Automatic").arg(aspectStr));
    }

    ui->axisMappingLabel->setVisible(false);
    ui->axisMappingCombo->setVisible(false);
    ui->axisMappingButton->setVisible(false);
  }
  else
  {
    ui->guessLabel->setVisible(false);
    ui->guessDetails1->setVisible(false);
    ui->guessDetails2->setVisible(false);
    ui->guessButton->setVisible(false);

    ui->axisMappingLabel->setVisible(true);
    ui->axisMappingCombo->setVisible(true);
    ui->axisMappingButton->setVisible(true);
    ui->axisMappingButton->setEnabled(ui->axisMappingCombo->currentIndex() == 4);
  }
}

void BufferViewer::on_outputTabs_currentChanged(int index)
{
  ui->renderContainer->parentWidget()->layout()->removeWidget(ui->renderContainer);
  ui->outputTabs->widget(index)->layout()->addWidget(ui->renderContainer);

  if(index == 0)
    m_CurStage = isMeshDraw() ? MeshDataStage::TaskOut : MeshDataStage::VSIn;
  else if(index == 1)
    m_CurStage = isMeshDraw() ? MeshDataStage::MeshOut : MeshDataStage::VSOut;
  else if(index == 2)
    m_CurStage = MeshDataStage::GSOut;

  configureDrawRange();

  ui->autofitCamera->setEnabled(!isCurrentRasterOut());

  UpdateStageDataControls();

  UpdateCurrentMeshConfig();
  UpdateMeshUV();

  INVOKE_MEMFN(RT_UpdateAndDisplay);
}

void BufferViewer::on_toggleControls_toggled(bool checked)
{
  ui->configurationGroup->setVisible(checked);

  // temporarily set minimum bounds to the longest float we could format, to ensure the minimum size
  // we calculate below is as big as needs to be (sigh...). This is necessary because Qt doesn't
  // properly propagate the minimum size up through the scroll area and instead sizes it down much
  // smaller.
  FloatVector prev = m_Config.minBounds;

  m_Config.minBounds.x = 1.0f;
  m_Config.minBounds.y = 1.2345e-20f;
  m_Config.minBounds.z = 123456.7890123456789f;
  m_Config.minBounds.w = 1.2345e+20f;

  UI_UpdateBoundingBoxLabels(4);

  m_Config.minBounds = prev;

  ui->cameraControlsWidget->setMinimumSize(ui->cameraControlsWidget->minimumSizeHint());
  ui->cameraControlsScroll->setMinimumWidth(ui->cameraControlsWidget->minimumSizeHint().width() +
                                            ui->cameraControlsScroll->verticalScrollBar()->width());

  UI_UpdateBoundingBoxLabels();

  UpdateStageDataControls();
}

void BufferViewer::on_syncViews_toggled(bool checked)
{
  SyncViews(NULL, true, true);
}

void BufferViewer::on_showPadding_toggled(bool checked)
{
  OnEventChanged(CBufferEvent());
}

void BufferViewer::on_highlightVerts_toggled(bool checked)
{
  UpdateHighlightVerts();

  INVOKE_MEMFN(RT_UpdateAndDisplay);
}

void BufferViewer::on_vtxExploderSlider_valueChanged(int value)
{
  m_Config.vtxExploderSliderSNorm = (float)value / 100.0f;
  UpdateMeshUVSelection();
}

void BufferViewer::on_exploderReset_clicked()
{
  ui->vtxExploderSlider->setSliderPosition(0);
}

void BufferViewer::on_exploderScale_valueChanged(double value)
{
  m_Config.exploderScale = (float)value;
  UpdateMeshUVSelection();
}

void BufferViewer::on_wireframeRender_toggled(bool checked)
{
  m_Config.wireframeDraw = checked;
  if(!checked && ui->visualisation->currentIndex() == int(Visualisation::NoSolid))
    ui->visualisation->setCurrentIndex(int(Visualisation::Solid));

  QueueMeshPreview();
}

void BufferViewer::on_visualisation_currentIndexChanged(int index)
{
  if(m_UVToggle && index >= 0 && index == UVTextureMode())
  {
    m_UVToggle->setChecked(true);
    UpdateMeshUVPreview();
    UpdateMeshUVSelection();
    return;
  }

  ui->wireframeRender->setEnabled(true);
  if(index == int(Visualisation::NoSolid))
    ui->wireframeRender->setChecked(true);

  bool explodeHidden = (index != (int)Visualisation::Explode);
  ui->vtxExploderLabel->setHidden(explodeHidden);
  ui->vtxExploderSlider->setHidden(explodeHidden);
  ui->exploderReset->setHidden(explodeHidden);
  ui->exploderScaleLabel->setHidden(explodeHidden);
  ui->exploderScale->setHidden(explodeHidden);
  UpdateHighlightVerts();

  m_Config.visualisationMode = (Visualisation)qMax(0, index);

  m_ModelIn->setSecondaryColumn(m_ModelIn->secondaryColumn(),
                                m_Config.visualisationMode == Visualisation::Secondary,
                                m_ModelIn->secondaryAlpha());
  m_ModelOut1->setSecondaryColumn(m_ModelOut1->secondaryColumn(),
                                  m_Config.visualisationMode == Visualisation::Secondary,
                                  m_ModelOut1->secondaryAlpha());
  m_ModelOut2->setSecondaryColumn(m_ModelOut2->secondaryColumn(),
                                  m_Config.visualisationMode == Visualisation::Secondary,
                                  m_ModelOut2->secondaryAlpha());

  if(m_UVData)
  {
    UpdateMeshUVPreview();
    UpdateMeshUVSelection();
  }
  else
    QueueMeshPreview();
}

void BufferViewer::on_drawRange_currentIndexChanged(int index)
{
  configureDrawRange();

  INVOKE_MEMFN(RT_UpdateAndDisplay);
}

void BufferViewer::on_controlType_currentIndexChanged(int index)
{
  m_Arcball->Reset(FloatVector(), 10.0f);
  m_Flycam->Reset(FloatVector());

  if(index == 0)
  {
    m_CurrentCamera = m_Arcball;

    UI_ResetArcball();
  }
  else
  {
    m_CurrentCamera = m_Flycam;
    if(isCurrentRasterOut())
      m_Flycam->Reset(FloatVector(0.0f, 0.0f, 0.0f, 0.0f));
    else
      m_Flycam->Reset(FloatVector(0.0f, 0.0f, -10.0f, 0.0f));
    if(!isCurrentRasterOut())
      FocusMeshSelection();
  }

  INVOKE_MEMFN(RT_UpdateAndDisplay);
}

void BufferViewer::on_resetCamera_clicked()
{
  if(isCurrentRasterOut())
    ui->controlType->setCurrentIndex(1);
  else
    ui->controlType->setCurrentIndex(0);

  // make sure callback is called even if we're re-selecting same
  // camera type
  on_controlType_currentIndexChanged(ui->controlType->currentIndex());
}

void BufferViewer::on_camSpeed_valueChanged(double value)
{
  m_Arcball->SpeedMultiplier = m_Flycam->SpeedMultiplier = value;
}

void BufferViewer::on_instance_valueChanged(int value)
{
  m_Config.curInstance = value;
  OnEventChanged(CBufferEvent());
}

void BufferViewer::on_viewIndex_valueChanged(int value)
{
  m_Config.curView = value;
  OnEventChanged(CBufferEvent());
}

void BufferViewer::SetMeshFilter(MeshFilter filter, uint32_t taskGroup, uint32_t meshGroup)
{
  // calculate new scrolls manually to keep the same logical item selected
  m_Scrolls = new PopulateBufferData;
  FillScrolls(m_Scrolls);

  {
    const BufferConfiguration &config1 = m_ModelOut1->getConfig();
    const BufferConfiguration &config2 = m_ModelOut2->getConfig();

    // baseTaskRow is the first row in the mesh view for the start of the task with no mesh filter,
    // and baseMeshRow is the offset to the filtered mesh (if relevant). They could be identical
    const uint32_t prevBaseTaskRow = m_TaskFilterRowOffset;
    const uint32_t prevBaseMeshRow = m_MeshFilterRowOffset;

    // if we're filtering directly to a task from none, we also have the prefix count we just have
    // to determine the base mesh
    uint32_t taskBaseMesh = 0;
    for(uint32_t i = 0; i < taskGroup && i < config1.taskSizes.size(); i++)
      taskBaseMesh += config1.taskSizes[i].x * config1.taskSizes[i].y * config1.taskSizes[i].z;

    uint32_t newBaseTaskRow = 0, newBaseMeshRow = 0;
    if(filter == MeshFilter::None || config2.meshletVertexPrefixCounts.empty())
    {
      // if the new filter is none, then our new base row for both is 0
      newBaseTaskRow = newBaseMeshRow = 0;
    }
    else if(m_CurMeshFilter == MeshFilter::None && filter == MeshFilter::Mesh)
    {
      newBaseTaskRow = config2.meshletVertexPrefixCounts[taskBaseMesh];
      newBaseMeshRow = config2.meshletVertexPrefixCounts[meshGroup];
    }
    else if(m_CurMeshFilter == MeshFilter::None && filter == MeshFilter::TaskGroup)
    {
      newBaseTaskRow = newBaseMeshRow = config2.meshletVertexPrefixCounts[taskBaseMesh];
    }
    else if(m_CurMeshFilter == MeshFilter::TaskGroup && filter == MeshFilter::Mesh)
    {
      // the first complex case - if we're already filtered to a task and now we're filtering to a
      // mesh, we only have prefix counts relatively so look it up
      newBaseTaskRow = prevBaseTaskRow;
      newBaseMeshRow = prevBaseTaskRow + config2.meshletVertexPrefixCounts[meshGroup - taskBaseMesh];
    }
    else if(m_CurMeshFilter == MeshFilter::Mesh && filter == MeshFilter::TaskGroup)
    {
      // the second complex case - if we're already filtered to a *mesh* and now we're filtering
      // back to the task, we undo the previous per-mesh filter
      newBaseTaskRow = newBaseMeshRow = prevBaseTaskRow;

      // only support filtering within the same group, not arbitrarily from one mesh in one task
      // group to a different task group
      Q_ASSERT(m_FilteredTaskGroup == taskGroup);
    }

    const uint32_t prevBaseRow = prevBaseMeshRow;
    const uint32_t newBaseRow = newBaseMeshRow;

    // when going to/from no filter, we just rebase by the base row and set the task row that we know directly
    if(m_CurMeshFilter == MeshFilter::None)
    {
      m_Scrolls->out1Vert = 0;
      m_Scrolls->out2Vert -= newBaseRow;
    }
    else if(filter == MeshFilter::None)
    {
      m_Scrolls->out1Vert = config1.taskOrMeshletOffset;
      m_Scrolls->out2Vert += prevBaseRow;
    }

    // otherwise changing between task and mesh filter, we rebase based on the difference between
    // the number of meshes shown. The task filter doesn't have to change
    else if(m_CurMeshFilter == MeshFilter::TaskGroup && filter == MeshFilter::Mesh)
    {
      m_Scrolls->out1Vert = 0;
      m_Scrolls->out2Vert -= (newBaseRow - prevBaseRow);
    }
    else if(m_CurMeshFilter == MeshFilter::Mesh && filter == MeshFilter::TaskGroup)
    {
      m_Scrolls->out1Vert = 0;
      m_Scrolls->out2Vert += (prevBaseRow - newBaseRow);
    }

    m_TaskFilterRowOffset = newBaseTaskRow;
    m_MeshFilterRowOffset = newBaseMeshRow;
  }

  m_CurMeshFilter = filter;
  m_FilteredTaskGroup = taskGroup;
  m_FilteredMeshGroup = meshGroup;

  switch(m_CurMeshFilter)
  {
    case MeshFilter::None:
      ui->meshFilterLabel->setText(tr("Current Range filter: None"));
      ui->resetMeshFilterButton->setEnabled(false);
      break;
    case MeshFilter::TaskGroup:
      if(IsD3D(m_Ctx.APIProps().pipelineType))
        ui->meshFilterLabel->setText(tr("Current Range filter: Single Amplification Threadgroup"));
      else
        ui->meshFilterLabel->setText(tr("Current Range filter: Single Task"));
      ui->resetMeshFilterButton->setEnabled(true);
      break;
    case MeshFilter::Mesh:
      ui->meshFilterLabel->setText(tr("Current Range filter: Single Meshlet"));
      ui->resetMeshFilterButton->setEnabled(true);
      break;
  }

  if(m_Ctx.IsCaptureLoaded())
    OnEventChanged(CBufferEvent());
}

void BufferViewer::on_rowOffset_valueChanged(int value)
{
  if(!m_MeshView && m_ModelIn->getConfig().unclampedNumRows > 0)
  {
    int page = value / MaxVisibleRows;
    value %= MaxVisibleRows;

    uint64_t pageOffset = page * MaxVisibleRows * m_ModelIn->getConfig().buffers[0]->stride;

    // account for the extra row at the top with previous/next buttons
    if(pageOffset > 0)
      value++;

    if(pageOffset != m_PagingByteOffset)
    {
      m_PagingByteOffset = pageOffset;

      processFormat(m_Format);

      return;
    }
  }

  ScrollToRow(ui->inTable, value);
  ScrollToRow(ui->out1Table, value);
  ScrollToRow(ui->out2Table, value);

  // when we're paging and we select the first row, actually scroll up to include the previous/next
  // buttons.
  if(!m_MeshView && value == 1 && m_PagingByteOffset > 0)
    ui->inTable->verticalScrollBar()->setValue(0);
}

void BufferViewer::on_autofitCamera_clicked()
{
  FocusMeshSelection();
}

#if ENABLE_UNIT_TESTS

#include "3rdparty/catch/catch.hpp"

TEST_CASE("Pinned CBuffer replay visits restore the user's event",
          "[bufferviewer][replaylab][cbuffer-pin]")
{
  struct ReplayEvents
  {
    uint32_t current = 101;
    QList<uint32_t> visits;
    uint32_t GetCurrentEventID() const { return current; }
    void SetFrameEvent(uint32_t eventId, bool force)
    {
      CHECK(force);
      current = eventId;
      visits.push_back(eventId);
    }
  } replay;

  SECTION("Unpinned and same-event operations do not replay")
  {
    { ScopedCBufferReplayEvent<ReplayEvents> event(&replay, 0); }
    { ScopedCBufferReplayEvent<ReplayEvents> event(&replay, 101); }
    CHECK(replay.visits.isEmpty());
    CHECK(replay.current == 101);
  }
  SECTION("An early return restores the current event")
  {
    const auto read = [&]() {
      ScopedCBufferReplayEvent<ReplayEvents> event(&replay, 17);
      REQUIRE(replay.current == 17);
      return;
    };
    read();
    REQUIRE(replay.visits.size() == 2);
    CHECK(replay.visits[0] == 17);
    CHECK(replay.visits[1] == 101);
    CHECK(replay.current == 101);
  }
  SECTION("Restoring before publication is idempotent")
  {
    {
      ScopedCBufferReplayEvent<ReplayEvents> event(&replay, 17);
      event.Restore();
      CHECK(replay.current == 101);
      event.Restore();
    }
    CHECK(replay.visits.size() == 2);
  }
}

TEST_CASE("CBuffer search resolves semantic names and original physical channels",
          "[bufferviewer][replaylab][cbuffer-search]")
{
  RDTreeWidgetItem group({QStringLiteral("Material"), QString(), QString(), QStringLiteral("struct")});
  RDTreeWidgetItem *color =
      new RDTreeWidgetItem({QStringLiteral("baseColor"), QStringLiteral("1, 0.5, 0.25"),
                            QStringLiteral("16, 20, 32"), QStringLiteral("float3")});
  FixedVarTag semantic;
  semantic.replayLabOriginalPaths = QStringList{QStringLiteral("cb5_c1_x"), QStringLiteral("cb5_c1_y"),
                                     QStringLiteral("cb5_c2_x")};
  semantic.replayLabEditOffsets = {16, 20, 32};
  color->setTag(QVariant::fromValue(semantic));
  RDTreeWidgetItem *roughness =
      new RDTreeWidgetItem({QStringLiteral("roughness"), QStringLiteral("0.75")});
  group.addChild(color);
  group.addChild(roughness);

  CHECK(CBufferSearchMatches(color, {QStringLiteral("BASEcolor"), QStringLiteral("float3")}));
  CHECK(CBufferSearchMatches(color, {QStringLiteral("cb5_c2_x"), QStringLiteral("0x20")}));
  CHECK_FALSE(CBufferSearchMatches(color, {QStringLiteral("roughness")}));
  CHECK_FALSE(CBufferSearchMatches(color, {QStringLiteral("baseColor"), QStringLiteral("float4")}));

  QSet<const RDTreeWidgetItem *> visible;
  CHECK(CBufferSearchVisibleItems(&group, {QStringLiteral("cb5_c2_x")}, visible));
  CHECK(visible.contains(&group));
  CHECK(visible.contains(color));
  CHECK_FALSE(visible.contains(roughness));
  visible.clear();
  CHECK(CBufferSearchVisibleItems(&group, {QStringLiteral("Material")}, visible));
  CHECK(visible.size() == 3);
  visible.clear();
  CHECK_FALSE(CBufferSearchVisibleItems(&group, {QStringLiteral("does-not-exist")}, visible));
  CHECK(visible.isEmpty());
  CHECK(CBufferSearchVisibleItems(&group, {}, visible));
  CHECK(visible.size() == 3);
}

TEST_CASE("CBuffer color square maps horizontal saturation and vertical brightness",
          "[bufferviewer][replaylab][cbuffer-color]")
{
  const QRectF area(7, 7, 200, 200);
  auto checkSV = [&area](const QPointF &point, qreal saturation, qreal value) {
    const QPointF actual = CBufferSVAtPoint(area, point);
    CHECK(actual.x() == saturation);
    CHECK(actual.y() == value);
  };
  checkSV(area.topLeft(), 0.0, 1.0);
  checkSV(area.topRight(), 1.0, 1.0);
  checkSV(area.bottomLeft(), 0.0, 0.0);
  checkSV(area.bottomRight(), 1.0, 0.0);
  checkSV(area.center(), 0.5, 0.5);
  checkSV(QPointF(-100, 999), 0.0, 0.0);
}

TEST_CASE("Live CBuffer edits preserve physical lanes and numeric precision",
          "[bufferviewer][replaylab][cbuffer-live-edit]")
{
  SECTION("reflected reads stay inside the declaration and the native binding")
  {
    CHECK(CBufferReflectedReadSize(530432, 1372) == 1372);
    CHECK(CBufferReflectedReadSize(2875904, 7572) == 7572);
    CHECK(CBufferReflectedReadSize(256, 1372) == 256);
    CHECK(CBufferReflectedReadSize(UINT64_MAX, 1372) == 1372);
    CHECK(CBufferReflectedReadSize(0, 1372) == 0);
    CHECK(CBufferReflectedReadSize(256, 0) == 256);
  }
  SECTION("adjacent vector channels submit once, scattered fields preserve their holes")
  {
    QMap<quint64, uint32_t> values;
    values[28] = 4;
    values[16] = 1;
    values[24] = 3;
    values[20] = 2;
    const QList<CBufferLiveRun> contiguous = CBufferLiveRuns(values);
    REQUIRE(contiguous.size() == 1);
    CHECK(contiguous[0].offset == 16);
    REQUIRE(contiguous[0].bytes.size() == 16);
    uint32_t physical[4] = {};
    memcpy(physical, contiguous[0].bytes.data(), 16);
    for(int i = 0; i < 4; i++)
      CHECK(physical[i] == uint32_t(i + 1));
    values.remove(20);
    const QList<CBufferLiveRun> scattered = CBufferLiveRuns(values);
    REQUIRE(scattered.size() == 2);
    CHECK(scattered[0].offset == 16);
    CHECK(scattered[0].bytes.size() == 4);
    CHECK(scattered[1].offset == 24);
    CHECK(scattered[1].bytes.size() == 8);
  }
  SECTION("HDR, negative and denormal floats remain exact and integers do not wrap")
  {
    uint32_t bits = 0;
    for(float input : {-7.25f, 16.5f, FLT_MIN, -FLT_MAX})
    {
      REQUIRE(CBufferLiveLaneBits(VarType::Float, double(input), bits));
      float actual;
      memcpy(&actual, &bits, 4);
      CHECK(actual == input);
    }
    CHECK(CBufferLiveLaneBits(VarType::UInt, double(UINT32_MAX), bits));
    CHECK(bits == UINT32_MAX);
    CHECK_FALSE(CBufferLiveLaneBits(VarType::UInt, -1, bits));
    CHECK_FALSE(CBufferLiveLaneBits(VarType::UInt, double(UINT32_MAX) + 1, bits));
    CHECK_FALSE(CBufferLiveLaneBits(VarType::SInt, 1.5, bits));
    CHECK(CBufferLiveLaneBits(VarType::SInt, double(INT32_MIN), bits));
    CHECK(bits == 0x80000000U);
    CHECK_FALSE(CBufferLiveLaneBits(VarType::Float, std::numeric_limits<double>::infinity(), bits));
    CHECK_FALSE(CBufferLiveLaneBits(VarType::Float, std::numeric_limits<double>::quiet_NaN(), bits));
  }
  SECTION("numeric editor text round-trips scientific notation and full integer ranges")
  {
    const QLocale locale = QLocale::c();
    for(float input : {1.0e-20f, std::numeric_limits<float>::denorm_min(), -7.25f, FLT_MAX})
    {
      bool valid = false;
      const double value = locale.toDouble(CBufferValueText(locale, double(input), false), &valid);
      CHECK(valid);
      CHECK(float(value) == input);
    }
    for(double input : {double(INT32_MIN), double(UINT32_MAX)})
      CHECK(locale.toDouble(CBufferValueText(locale, input, true)) == input);
  }
  SECTION("a vector is not automatically treated as a color")
  {
    CHECK(CBufferColorField(QStringLiteral("baseTint"), VarType::Float, 3));
    CHECK(CBufferColorField(QStringLiteral("baseColor"), VarType::Float, 4));
    CHECK_FALSE(CBufferColorField(QStringLiteral("surfaceNormal"), VarType::Float, 3));
    CHECK_FALSE(CBufferColorField(QStringLiteral("colorIndex"), VarType::UInt, 4));
    CHECK_FALSE(CBufferColorField(QStringLiteral("colorStrength"), VarType::Float, 1));
  }
}

TEST_CASE("CBuffer semantic projection preserves physical lanes and user names",
          "[bufferviewer][replaylab][cbuffer-semantics]")
{
  RDTreeWidgetItem root;
  auto add = [&](const QString &name, uint32_t offset, int columns, VarType type = VarType::Float) {
    ShaderVariable value;
    value.name = name;
    value.type = type;
    value.rows = 1;
    value.columns = uint8_t(columns);
    for(int col = 0; col < columns; col++)
      value.value.f32v[col] = float(offset + col * 4) / 16.0f;
    ShaderConstant constant;
    constant.type.baseType = type;
    constant.type.rows = 1;
    constant.type.columns = uint8_t(columns);
    FixedVarTag tag = ReplayLabFixedVarTag(constant, value, offset);
    tag.path = name;
    tag.leaf = true;
    RDTreeWidgetItem *item = new RDTreeWidgetItem(
        {name, VarString(value, constant), QString::number(offset), TypeString(value, constant)});
    item->setTag(QVariant::fromValue(tag));
    root.addChild(item);
    return item;
  };
  auto field = [](const QString &name, const QString &type, QList<qint64> offsets) {
    ReplayLabCBufferSemanticField result;
    result.name = name;
    result.type = type;
    result.offsets = offsets;
    result.status = QStringLiteral("inferred");
    result.provenance = QStringLiteral("shader");
    return result;
  };
  auto addMatrix = [&](bool rowMajor, uint32_t base = 64) {
    ShaderConstant constant;
    constant.type.baseType = VarType::Float;
    constant.type.rows = 3;
    constant.type.columns = 2;
    constant.type.matrixByteStride = 16;
    if(rowMajor)
      constant.type.flags |= ShaderVariableFlags::RowMajorMatrix;
    ShaderVariable value;
    value.name = "basis";
    value.type = VarType::Float;
    value.rows = 3;
    value.columns = 2;
    value.flags = constant.type.flags;
    for(int i = 0; i < 6; i++)
      value.value.f32v[i] = float(i + 1);
    FixedVarTag parentTag = ReplayLabFixedVarTag(constant, value, base);
    parentTag.path = QStringLiteral("instances[1].basis");
    parentTag.leaf = parentTag.matrix = true;
    RDTreeWidgetItem *parent = new RDTreeWidgetItem({parentTag.path});
    parent->setTag(QVariant::fromValue(parentTag));
    root.addChild(parent);
    for(uint32_t row = 0; row < 3; row++)
    {
      const FixedVarTag tag = ReplayLabMatrixRowTag(constant, value, parentTag, row);
      RDTreeWidgetItem *item = new RDTreeWidgetItem({tag.path});
      item->setTag(QVariant::fromValue(tag));
      parent->addChild(item);
    }
    return parent;
  };

  SECTION("Chinese semantic labels display unchanged and preserve the user's name")
  {
    const QString matrixLabel = QStringLiteral("\u4e16\u754c\u5230\u89c6\u7a7a\u95f4[0]\u00b7\u5b58\u50a8\u884c");
    const QString tintLabel = QStringLiteral("\u5e95\u8272 \u67d3\u8272");
    const QString userLabel = QStringLiteral("\u6211\u7684\u989c\u8272");
    add(QStringLiteral("physicalMatrix"), 0, 4);
    add(QStringLiteral("physicalTint"), 16, 4);
    RDTreeWidgetItem *user = add(QStringLiteral("physicalUserValue"), 32, 4);
    FixedVarTag tag = user->tag().value<FixedVarTag>();
    tag.alias = userLabel;
    tag.aliasSource = QStringLiteral("user");
    user->setTag(QVariant::fromValue(tag));
    user->setText(0, userLabel);
    CHECK(ApplyCBufferSemanticFields(&root, {
        field(matrixLabel, QStringLiteral("float4"), {0, 4, 8, 12}),
        field(tintLabel, QStringLiteral("float4"), {16, 20, 24, 28}),
        field(QStringLiteral("AI tint"), QStringLiteral("float4"), {32, 36, 40, 44})}) == 2);
    REQUIRE(root.childCount() == 3);
    QSet<QString> labels;
    for(int i = 0; i < root.childCount(); i++)
      labels.insert(root.child(i)->text(0));
    CHECK(labels.contains(matrixLabel));
    CHECK(labels.contains(tintLabel));
    CHECK(labels.contains(userLabel));
    CHECK(bool(user->tag().value<FixedVarTag>().path == QStringLiteral("physicalUserValue")));
    CHECK(bool(user->tag().value<FixedVarTag>().replayLabEditOffsets == QList<quint64>{32, 36, 40, 44}));
  }

  SECTION("AI translations retain English source identifiers and editable lanes")
  {
    add(QStringLiteral("materialControls_smoothness"), 0, 1);
    add(QStringLiteral("materialControls_gradeTintBlend"), 16, 4);
    ReplayLabCBufferSemanticField smooth = field(QStringLiteral("\u5149\u6ed1\u5ea6"),
                                               QStringLiteral("float"), {0});
    ReplayLabCBufferSemanticField tint = field(QStringLiteral("\u8c03\u8272\u8272\u8c03"),
                                             QStringLiteral("float3"), {16, 20, 24});
    smooth.source = tint.source = QStringLiteral("ai");
    REQUIRE(ApplyCBufferSemanticFields(&root, {smooth, tint}) == 2);
    CHECK(bool(root.child(0)->text(0) == QStringLiteral("materialControls_smoothness")));
    CHECK(bool(root.child(0)->tag().value<FixedVarTag>().alias == smooth.name));
    CHECK(bool(root.child(0)->tag().value<FixedVarTag>().replayLabEditOffsets == QList<quint64>{0}));
    CHECK(CBufferSearchMatches(root.child(0), {smooth.name}));
    CHECK(CBufferSearchMatches(root.child(0), {QStringLiteral("smoothness")}));
    RDTreeWidgetItem *color = NULL;
    for(int i = 0; i < root.childCount(); ++i)
      if(root.child(i)->text(0) == QStringLiteral("materialControls_gradeTintBlend.xyz"))
        color = root.child(i);
    REQUIRE(color);
    CHECK(bool(color->tag().value<FixedVarTag>().replayLabEditOffsets == QList<quint64>{16, 20, 24}));
  }

  SECTION("captured placeholders use typed replacement source identifiers")
  {
    add(QStringLiteral("_child0"), 0, 1);
    add(QStringLiteral("_child1"), 4, 1);
    add(QStringLiteral("_child2"), 8, 1);
    add(QStringLiteral("_child3"), 12, 1);
    ShaderConstant tint;
    tint.name = "materialControls_baseTint";
    tint.type.baseType = VarType::Float;
    tint.type.rows = 1;
    tint.type.columns = 4;
    tint.type.elements = 1;
    auto semantic = field(QStringLiteral("\u5e95\u8272"), QStringLiteral("float3"), {0, 4, 8});
    semantic.source = QStringLiteral("ai");
    REQUIRE(ApplyCBufferSemanticFields(&root, {semantic}, {tint}) == 1);
    CHECK(bool(root.child(0)->text(0) == QStringLiteral("materialControls_baseTint.xyz")));
    CHECK(bool(root.child(0)->tag().value<FixedVarTag>().replayLabEditOffsets == QList<quint64>{0, 4, 8}));
    CHECK(CBufferSearchMatches(root.child(0), {semantic.name}));
    tint.type.baseType = VarType::UInt;
    const auto lanes = CBufferSourceNames({tint});
    CHECK(lanes.value(0).type == VarType::UInt);
    // The independent physical type check above still rejects a differently typed source.
    tint.type.members = {tint};
    tint.type.elements = UINT32_MAX;
    CHECK(CBufferSourceNames({tint}).isEmpty());
  }

  SECTION("unused and unresolved physical fields stay distinct and editable")
  {
    RDTreeWidgetItem *unused = add(QStringLiteral("_child4"), 16, 1);
    RDTreeWidgetItem *unknown = add(QStringLiteral("_child6"), 24, 1);
    RDTreeWidgetItem *mismatch = add(QStringLiteral("_child7"), 28, 1);
    RDTreeWidgetItem *user = add(QStringLiteral("_child8"), 32, 1);
    FixedVarTag userTag = user->tag().value<FixedVarTag>();
    userTag.aliasSource = QStringLiteral("user");
    user->setTag(QVariant::fromValue(userTag));
    CHECK(DescribeCBufferUnnamedFields(&root, {{16, QStringLiteral("float")},
                                             {28, QStringLiteral("uint")},
                                             {32, QStringLiteral("float")}}));
    CHECK(bool(unused->text(0) == QCoreApplication::translate("BufferViewer", "Unused · %1").arg(16)));
    CHECK(bool(unknown->text(0) == QCoreApplication::translate("BufferViewer", "Parameter · %1").arg(24)));
    CHECK(bool(mismatch->text(4) == QCoreApplication::translate("BufferViewer", "Meaning not identified")));
    CHECK(bool(user->text(0) == QStringLiteral("_child8")));
    const FixedVarTag preserved = unused->tag().value<FixedVarTag>();
    CHECK(bool(preserved.path == QStringLiteral("_child4")));
    CHECK(bool(preserved.replayLabEditOffsets == QList<quint64>{16}));
    CHECK(preserved.replayLabDisplayValue.value.f32v[0] == 1.0f);
  }
  SECTION("column-major rows preserve logical values and strided physical edit offsets")
  {
    RDTreeWidgetItem *matrix = addMatrix(false);
    const FixedVarTag row = matrix->child(1)->tag().value<FixedVarTag>();
    CHECK_FALSE(row.valid);
    CHECK_FALSE(row.leaf);
    CHECK(CanAttachCBufferParameter(row));
    CHECK(bool(row.replayLabEditOffsets == QList<quint64>{68, 84}));
    CHECK(row.replayLabDisplayValue.value.f32v[0] == 3.0f);
    CHECK(row.replayLabDisplayValue.value.f32v[1] == 4.0f);
    CHECK(ApplyCBufferSemanticFields(&root, {
        field(QStringLiteral("basis.x"), QStringLiteral("float2"), {64, 80}),
        field(QStringLiteral("basis.y"), QStringLiteral("float2"), {68, 84}),
        field(QStringLiteral("basis.z"), QStringLiteral("float2"), {72, 88})}) == 3);
    REQUIRE(root.childCount() == 3);
    const FixedVarTag projected = root.child(1)->tag().value<FixedVarTag>();
    CHECK(bool(projected.replayLabEditOffsets == QList<quint64>{68, 84}));
    CHECK(projected.replayLabDisplayValue.value.f32v[1] == 4.0f);
  }
  SECTION("matrix padding is not projected and partial rows retain the right physical lanes")
  {
    RDTreeWidgetItem *matrix = addMatrix(false);
    CHECK(ApplyCBufferSemanticFields(&root, {
        field(QStringLiteral("padding"), QStringLiteral("float"), {76})}) == 0);
    CHECK(ApplyCBufferSemanticFields(&root, {
        field(QStringLiteral("coefficient"), QStringLiteral("float"), {64})}) == 1);
    REQUIRE(matrix->childCount() == 3);
    const FixedVarTag remaining = matrix->child(0)->tag().value<FixedVarTag>();
    CHECK(remaining.replayLabComponents == 1);
    CHECK(bool(remaining.replayLabEditOffsets == QList<quint64>{80}));
    CHECK(remaining.replayLabDisplayValue.value.f32v[0] == 2.0f);
    CHECK(remaining.byteOffset == 80);
  }
  SECTION("row-major arrays use the enclosing element offset and preserve user aliases")
  {
    RDTreeWidgetItem *matrix = addMatrix(true, 256);
    const FixedVarTag row = matrix->child(2)->tag().value<FixedVarTag>();
    CHECK(bool(row.replayLabEditOffsets == QList<quint64>{288, 292}));
    FixedVarTag parentTag = matrix->tag().value<FixedVarTag>();
    parentTag.aliasSource = QStringLiteral("user");
    matrix->setTag(QVariant::fromValue(parentTag));
    CHECK(ApplyCBufferSemanticFields(&root, {
        field(QStringLiteral("basis.x"), QStringLiteral("float2"), {256, 260})}) == 0);
    REQUIRE(root.childCount() == 1);
    REQUIRE(matrix->childCount() == 3);
  }
  SECTION("matrix projection rejects truncated values and invalid physical strides")
  {
    ShaderConstant constant;
    constant.type.baseType = VarType::Float;
    constant.type.rows = constant.type.columns = 3;
    constant.type.matrixByteStride = 8;
    ShaderVariable value;
    value.type = VarType::Float;
    value.rows = value.columns = 3;
    const FixedVarTag parent = ReplayLabFixedVarTag(constant, value, 0);
    CHECK(ReplayLabMatrixRowTag(constant, value, parent, 0).replayLabEditOffsets.isEmpty());
    constant.type.matrixByteStride = 16;
    value.flags |= ShaderVariableFlags::Truncated;
    CHECK(ReplayLabMatrixRowTag(constant, value, parent, 0).replayLabEditOffsets.isEmpty());
  }

  SECTION("scalar declarations combine in explicitly requested channel order")
  {
    for(int i = 0; i < 4; i++)
      add(QStringLiteral("scalar%1").arg(i), 16 + i * 4, 1);
    add(QStringLiteral("unrelated"), 48, 1);
    CHECK(ApplyCBufferSemanticFields(&root, {field(QStringLiteral("baseColor"),
                                                 QStringLiteral("float4"), {28, 16, 24, 20})}) == 1);
    REQUIRE(root.childCount() == 2);
    CHECK(bool(root.child(0)->text(0) == QStringLiteral("baseColor")));
    CHECK(bool(root.child(0)->text(2) == QStringLiteral("28, 16, 24, 20")));
    CHECK(bool(root.child(0)->text(3) == QStringLiteral("float4")));
    CHECK_FALSE(root.child(0)->tag().value<FixedVarTag>().valid);
    const FixedVarTag direct = root.child(0)->tag().value<FixedVarTag>();
    CHECK(CanAttachCBufferParameter(direct));
    CHECK(CanAttachCBufferParameter(root.child(1)->tag().value<FixedVarTag>()));
    CHECK_FALSE(CanAttachCBufferParameter(FixedVarTag()));
    CHECK_FALSE(CanAttachCBufferParameter(FixedVarTag(16)));
    REQUIRE(direct.replayLabEditOffsets.size() == 4);
    CHECK(direct.replayLabEditOffsets[0] == 28);
    CHECK(direct.replayLabEditOffsets[1] == 16);
    CHECK(direct.replayLabDisplayValue.value.f32v[0] == 1.75f);
    CHECK(bool(root.child(0)->text(1).startsWith(QStringLiteral("1.75"))));
    CHECK(bool(root.child(1)->text(0) == QStringLiteral("unrelated")));
  }
  SECTION("repeated array roles group without losing physical edit or search identities")
  {
    add(QStringLiteral("records[0].packed"), 0, 1);
    add(QStringLiteral("records[1].packed"), 16, 1);
    CHECK(ApplyCBufferSemanticFields(&root, {
        field(QStringLiteral("roughness[0]"), QStringLiteral("float"), {0}),
        field(QStringLiteral("roughness[1]"), QStringLiteral("float"), {16})}) == 2);
    REQUIRE(root.childCount() == 1);
    RDTreeWidgetItem *group = root.child(0);
    CHECK(bool(group->text(0) == QStringLiteral("roughness")));
    CHECK_FALSE(CanAttachCBufferParameter(group->tag().value<FixedVarTag>()));
    REQUIRE(group->childCount() == 2);
    CHECK(bool(group->child(1)->text(0) == QStringLiteral("1")));
    const FixedVarTag tag = group->child(1)->tag().value<FixedVarTag>();
    CHECK(CanAttachCBufferParameter(tag));
    CHECK(bool(tag.replayLabEditOffsets == QList<quint64>{16}));
    CHECK(bool(tag.replayLabOriginalPaths == QStringList{QStringLiteral("records[1].packed")}));
    CHECK(CBufferSearchMatches(group->child(1), {QStringLiteral("roughness[1]")}));
    CHECK(CBufferSearchMatches(group->child(1), {QStringLiteral("records[1].packed")}));
  }
  SECTION("unproven array labels stay flat and unambiguous")
  {
    add(QStringLiteral("scalar0"), 0, 1);
    add(QStringLiteral("scalar1"), 16, 1);
    CHECK(ApplyCBufferSemanticFields(&root, {
        field(QStringLiteral("roughness[0]"), QStringLiteral("float"), {0}),
        field(QStringLiteral("roughness[1]"), QStringLiteral("float"), {16})}) == 2);
    REQUIRE(root.childCount() == 2);
    CHECK(bool(root.child(1)->text(0) == QStringLiteral("roughness[1]")));
    CHECK(root.child(1)->childCount() == 0);
  }
  SECTION("one visible array element keeps its identity without a redundant parent")
  {
    add(QStringLiteral("records[1].packed"), 16, 1);
    CHECK(ApplyCBufferSemanticFields(&root, {
        field(QStringLiteral("roughness[1]"), QStringLiteral("float"), {16})}) == 1);
    REQUIRE(root.childCount() == 1);
    CHECK(bool(root.child(0)->text(0) == QStringLiteral("roughness")));
    CHECK(root.child(0)->childCount() == 0);
    CHECK(CBufferSearchMatches(root.child(0), {QStringLiteral("roughness[1]")}));
    CHECK(CBufferSearchMatches(root.child(0), {QStringLiteral("records[1].packed")}));
  }
  SECTION("a single array element retains its qualifier when the short name already exists")
  {
    add(QStringLiteral("roughness"), 0, 1);
    add(QStringLiteral("records[1].packed"), 16, 1);
    CHECK(ApplyCBufferSemanticFields(&root, {
        field(QStringLiteral("roughness[1]"), QStringLiteral("float"), {16})}) == 1);
    REQUIRE(root.childCount() == 2);
    CHECK(bool(root.child(1)->text(0) == QStringLiteral("roughness[1]")));
  }
  SECTION("array role groups preserve matrix row suffixes and physical ordering")
  {
    add(QStringLiteral("records[1].basis.row0"), 0, 4);
    add(QStringLiteral("records[1].basis.row1"), 16, 4);
    add(QStringLiteral("records[2].basis.row0"), 32, 4);
    CHECK(ApplyCBufferSemanticFields(&root, {
        field(QStringLiteral("objectToWorld[2].row0"), QStringLiteral("float4"), {32, 36, 40, 44}),
        field(QStringLiteral("objectToWorld[1].row1"), QStringLiteral("float4"), {16, 20, 24, 28}),
        field(QStringLiteral("objectToWorld[1].row0"), QStringLiteral("float4"), {0, 4, 8, 12})}) == 3);
    REQUIRE(root.childCount() == 1);
    RDTreeWidgetItem *group = root.child(0);
    CHECK(bool(group->text(0) == QStringLiteral("objectToWorld")));
    REQUIRE(group->childCount() == 3);
    CHECK(bool(group->child(0)->text(0) == QStringLiteral("1.row0")));
    CHECK(bool(group->child(1)->text(0) == QStringLiteral("1.row1")));
    CHECK(bool(group->child(2)->text(0) == QStringLiteral("2.row0")));
  }
  SECTION("an existing role name prevents a colliding generated array group")
  {
    add(QStringLiteral("roughness"), 32, 1);
    add(QStringLiteral("records[0].packed"), 0, 1);
    add(QStringLiteral("records[1].packed"), 16, 1);
    CHECK(ApplyCBufferSemanticFields(&root, {
        field(QStringLiteral("roughness[0]"), QStringLiteral("float"), {0}),
        field(QStringLiteral("roughness[1]"), QStringLiteral("float"), {16})}) == 2);
    REQUIRE(root.childCount() == 3);
    for(int i = 0; i < root.childCount(); ++i)
      CHECK(root.child(i)->childCount() == 0);
  }
  SECTION("user semantic names never acquire automatic grouping")
  {
    add(QStringLiteral("records[0].packed"), 0, 1);
    add(QStringLiteral("records[1].packed"), 16, 1);
    ReplayLabCBufferSemanticField first = field(QStringLiteral("roughness[0]"), QStringLiteral("float"), {0});
    ReplayLabCBufferSemanticField second = field(QStringLiteral("roughness[1]"), QStringLiteral("float"), {16});
    first.source = second.source = QStringLiteral("user");
    CHECK(ApplyCBufferSemanticFields(&root, {first, second}) == 2);
    REQUIRE(root.childCount() == 2);
    CHECK(bool(root.child(1)->text(0) == QStringLiteral("roughness[1]")));
  }
  SECTION("packed float4 splits by meaning and preserves unused channels")
  {
    add(QStringLiteral("packed"), 32, 4);
    CHECK(ApplyCBufferSemanticFields(&root, {field(QStringLiteral("roughness"),
                                                 QStringLiteral("float"), {36})}) == 1);
    REQUIRE(root.childCount() == 4);
    CHECK(bool(root.child(0)->text(0) == QStringLiteral("packed.x")));
    CHECK(bool(root.child(1)->text(0) == QStringLiteral("roughness")));
    CHECK(bool(root.child(1)->text(1).startsWith(QStringLiteral("2.25"))));
    CHECK(bool(root.child(2)->text(0) == QStringLiteral("packed.z")));
    CHECK(bool(root.child(3)->text(0) == QStringLiteral("packed.w")));
    CHECK_FALSE(root.child(0)->tag().value<FixedVarTag>().replayLabMaterialLeaf);
    REQUIRE(root.child(0)->tag().value<FixedVarTag>().replayLabEditOffsets.size() == 1);
    CHECK(root.child(0)->tag().value<FixedVarTag>().replayLabEditOffsets[0] == 32);
  }
  SECTION("missing lanes, type changes and overlapping reflection cannot hide source data")
  {
    add(QStringLiteral("packed"), 32, 4);
    add(QStringLiteral("overlap"), 36, 1);
    CHECK(ApplyCBufferSemanticFields(&root, {
        field(QStringLiteral("missing"), QStringLiteral("float2"), {32, 64}),
        field(QStringLiteral("bitcast"), QStringLiteral("uint"), {40}),
        field(QStringLiteral("ambiguous"), QStringLiteral("float"), {36})}) == 0);
    CHECK(root.childCount() == 2);
    CHECK(bool(root.child(0)->text(0) == QStringLiteral("packed")));
  }
  SECTION("one packed row can supply several independent semantic parameters")
  {
    add(QStringLiteral("packed"), 32, 4);
    CHECK(ApplyCBufferSemanticFields(&root, {
        field(QStringLiteral("roughness"), QStringLiteral("float"), {36}),
        field(QStringLiteral("tint"), QStringLiteral("float3"), {32, 40, 44})}) == 2);
    REQUIRE(root.childCount() == 2);
    CHECK(bool(root.child(0)->text(0) == QStringLiteral("tint")));
    CHECK(bool(root.child(0)->text(2) == QStringLiteral("32, 40, 44")));
    CHECK(bool(root.child(1)->text(0) == QStringLiteral("roughness")));
  }
  SECTION("user aliases take priority over semantic proposals")
  {
    RDTreeWidgetItem *item = add(QStringLiteral("packed"), 32, 4);
    FixedVarTag tag = item->tag().value<FixedVarTag>();
    tag.aliasSource = QStringLiteral("user");
    item->setTag(QVariant::fromValue(tag));
    CHECK(ApplyCBufferSemanticFields(&root, {field(QStringLiteral("roughness"),
                                                 QStringLiteral("float"), {36})}) == 0);
    CHECK(root.childCount() == 1);
    CHECK(root.child(0) == item);
  }
  SECTION("truncated reflection values remain unavailable instead of becoming zero")
  {
    RDTreeWidgetItem *item = add(QStringLiteral("partial"), 32, 4);
    FixedVarTag tag = item->tag().value<FixedVarTag>();
    tag.replayLabDisplayValue.flags |= ShaderVariableFlags::Truncated;
    item->setTag(QVariant::fromValue(tag));
    CHECK(ApplyCBufferSemanticFields(&root, {field(QStringLiteral("roughness"),
                                                 QStringLiteral("float"), {36})}) == 0);
    CHECK(root.childCount() == 1);
    CHECK(root.child(0) == item);
  }
  SECTION("user-named arrays and structs preserve their existing grouping")
  {
    RDTreeWidgetItem *item = add(QStringLiteral("packed[0]"), 32, 4);
    root.takeChild(0);
    RDTreeWidgetItem *parent = new RDTreeWidgetItem({QStringLiteral("User material")});
    FixedVarTag tag;
    tag.aliasSource = QStringLiteral("user");
    parent->setTag(QVariant::fromValue(tag));
    parent->addChild(item);
    root.addChild(parent);
    CHECK(ApplyCBufferSemanticFields(&root, {field(QStringLiteral("roughness"),
                                                 QStringLiteral("float"), {36})}) == 0);
    REQUIRE(root.childCount() == 1);
    CHECK(root.child(0) == parent);
    REQUIRE(parent->childCount() == 1);
    CHECK(parent->child(0) == item);
  }
  SECTION("large instance arrays preserve every lane in physical order when grouped")
  {
    // A 64 KiB instance buffer mirrors the large real capture which used to
    // spend most of its UI time repeatedly scanning/copying previously added rows.
    const int instances = 4096;
    QList<ReplayLabCBufferSemanticField> fields;
    fields.reserve(instances * 2);
    for(int instance = 0; instance < instances; ++instance)
      add(QStringLiteral("instances[%1].packed").arg(instance), uint32_t(instance * 16), 4);
    for(int instance = instances - 1; instance >= 0; --instance)
    {
      const qint64 offset = instance * 16;
      fields.push_back(field(QStringLiteral("roughness[%1]").arg(instance),
                             QStringLiteral("float"), {offset + 12}));
      fields.push_back(field(QStringLiteral("baseColor[%1]").arg(instance),
                             QStringLiteral("float3"), {offset, offset + 4, offset + 8}));
    }
    QElapsedTimer clock;
    clock.start();
    REQUIRE(ApplyCBufferSemanticFields(&root, fields) == instances * 2);
    qInfo() << "CBuffer semantic projection:" << instances * 2 << "fields in"
            << clock.elapsed() << "ms";
    REQUIRE(root.childCount() == 2);
    REQUIRE(bool(root.child(0)->text(0) == QStringLiteral("baseColor")));
    REQUIRE(bool(root.child(1)->text(0) == QStringLiteral("roughness")));
    REQUIRE(root.child(0)->childCount() == instances);
    REQUIRE(root.child(1)->childCount() == instances);
    for(int instance : {0, 1, instances / 2, instances - 1})
    {
      const RDTreeWidgetItem *color = root.child(0)->child(instance);
      const RDTreeWidgetItem *roughness = root.child(1)->child(instance);
      CHECK(bool(color->text(0) == QString::number(instance)));
      const FixedVarTag colorTag = color->tag().value<FixedVarTag>();
      const FixedVarTag roughnessTag = roughness->tag().value<FixedVarTag>();
      CHECK(bool(colorTag.replayLabEditOffsets ==
                 QList<quint64>{quint64(instance * 16), quint64(instance * 16 + 4),
                                quint64(instance * 16 + 8)}));
      CHECK(bool(roughnessTag.replayLabEditOffsets == QList<quint64>{quint64(instance * 16 + 12)}));
      CHECK(colorTag.replayLabDisplayValue.value.f32v[2] == float(instance) + 0.5f);
      CHECK(roughnessTag.replayLabDisplayValue.value.f32v[0] == float(instance) + 0.75f);
    }
    RDTreeWidgetItem destination;
    AppendCBufferChildren(&destination, &root);
    CHECK(root.childCount() == 0);
    REQUIRE(destination.childCount() == 2);
    CHECK(destination.child(0)->parent() == &destination);
    CHECK(destination.child(0)->child(instances - 1)->parent() == destination.child(0));
  }
}

TEST_CASE("ReplayLab material gate accepts reflected shared storage across replay APIs",
          "[bufferviewer][replaylab][material]")
{
  ReplayLabMaterialGateState state;
  CHECK(int(GetReplayLabMaterialGate(state)) == int(ReplayLabMaterialGate::Available));

  SECTION("a local D3D12 CBV-backed draw is supported")
  {
    state.api = GraphicsAPI::D3D12;
    state.registerSpace = 9;
    CHECK(int(GetReplayLabMaterialGate(state)) == int(ReplayLabMaterialGate::Available));
    state.byteOffset = 16;
    CHECK(int(GetReplayLabMaterialGate(state)) ==
          int(ReplayLabMaterialGate::InvalidBindingWindow));
  }

  SECTION("a D3D12 root CBV uses reflection instead of the upload-ring remainder")
  {
    const uint64_t ringRemainder = 8ULL * 1024ULL * 1024ULL;
    CHECK(ReplayLabMaterialBindingByteSize(GraphicsAPI::D3D12, ringRemainder, 48, true) ==
          48);
    CHECK(ReplayLabMaterialBindingByteSize(GraphicsAPI::D3D12, 256, 48, false) == 256);
    CHECK(ReplayLabMaterialBindingByteSize(GraphicsAPI::D3D11, 64, 48, true) == 64);
  }

  SECTION("Present and bookkeeping EIDs explicitly request a real action")
  {
    state.api = GraphicsAPI::D3D12;
    state.replayableAction = false;
    CHECK(int(GetReplayLabMaterialGate(state)) ==
          int(ReplayLabMaterialGate::NotReplayableAction));
  }

  SECTION("D3D12 root constants remain excluded")
  {
    state.api = GraphicsAPI::D3D12;
    state.inlineData = true;
    CHECK(int(GetReplayLabMaterialGate(state)) == int(ReplayLabMaterialGate::RootConstants));
  }

  SECTION("D3D12 indirect selection addresses a resource range rather than overriding a root slot")
  {
    state.api = GraphicsAPI::D3D12;
    state.rootCBV = true;
    state.indirectAction = true;
    CHECK(int(GetReplayLabMaterialGate(state)) == int(ReplayLabMaterialGate::Available));
    state.rootCBV = false;
    CHECK(int(GetReplayLabMaterialGate(state)) == int(ReplayLabMaterialGate::Available));
  }

  SECTION("remote replay uses the same shared storage contract")
  {
    state.remoteReplay = true;
    CHECK(int(GetReplayLabMaterialGate(state)) == int(ReplayLabMaterialGate::Available));
  }

  SECTION("GL and Vulkan selected buffer elements do not inherit D3D window limits")
  {
    for(GraphicsAPI api : {GraphicsAPI::OpenGL, GraphicsAPI::Vulkan})
    {
      state.api = api;
      state.remoteReplay = true;
      state.byteOffset = (1ULL << 33) + 4;
      state.byteSize = 65540;
      state.bindArraySize = 4;
      state.arrayIndex = 3;
      state.physicalSlot = 27;
      state.registerSpace = 2;
      CHECK(int(GetReplayLabMaterialGate(state)) == int(ReplayLabMaterialGate::Available));
      state.arrayIndex = 4;
      CHECK(int(GetReplayLabMaterialGate(state)) == int(ReplayLabMaterialGate::UnsupportedBinding));
      state.bindArraySize = 0;
      CHECK(int(GetReplayLabMaterialGate(state)) == int(ReplayLabMaterialGate::Available));
      state.byteOffset = UINT64_MAX - 8;
      CHECK(int(GetReplayLabMaterialGate(state)) == int(ReplayLabMaterialGate::InvalidBindingWindow));
    }
  }

  SECTION("a custom UI format is not treated as captured reflection")
  {
    state.customFormat = true;
    CHECK(int(GetReplayLabMaterialGate(state)) == int(ReplayLabMaterialGate::CustomFormat));
  }

  SECTION("unbound or malformed runtime windows stay disabled")
  {
    state.bufferBound = false;
    CHECK(int(GetReplayLabMaterialGate(state)) == int(ReplayLabMaterialGate::BufferUnbound));
    state.bufferBound = true;
    state.byteSize = UINT64_MAX;
    CHECK(int(GetReplayLabMaterialGate(state)) ==
          int(ReplayLabMaterialGate::InvalidBindingWindow));
  }

  SECTION("the fixed D3D11 binding contract is enforced")
  {
    state.physicalSlot = 14;
    CHECK(int(GetReplayLabMaterialGate(state)) ==
          int(ReplayLabMaterialGate::UnsupportedBinding));
    state.physicalSlot = 0;
    state.registerSpace = 1;
    CHECK(int(GetReplayLabMaterialGate(state)) ==
          int(ReplayLabMaterialGate::UnsupportedBinding));
  }
}

#endif    // ENABLE_UNIT_TESTS
