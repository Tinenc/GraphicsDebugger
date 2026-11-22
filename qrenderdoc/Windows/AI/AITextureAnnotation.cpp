/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2026 The RenderDoc Authors
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

#include "AITextureAnnotation.h"
#include "3rdparty/flowlayout/FlowLayout.h"

#include <QButtonGroup>
#include <QColorDialog>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QLabel>
#include <QMouseEvent>
#include <QPainter>
#include <QPushButton>
#include <QResizeEvent>
#include <QShortcut>
#include <QSpinBox>
#include <QToolButton>
#include <QVBoxLayout>
#include <QWheelEvent>
#include <cmath>

namespace
{
enum class MarkTool
{
  Pen,
  Ellipse,
  Arrow,
  Eraser
};
struct Mark
{
  MarkTool tool = MarkTool::Pen;
  QVector<QPointF> points;
  QColor color = QColor(255, 64, 80);
  double width = 0.003;
};

QPainterPath markPath(const Mark &mark, const QRectF &rect)
{
  QPainterPath path;
  if(mark.points.isEmpty())
    return path;
  const auto map = [&rect](QPointF p) {
    return QPointF(rect.left() + p.x() * rect.width(), rect.top() + p.y() * rect.height());
  };
  const QPointF start = map(mark.points.first()), end = map(mark.points.last());
  if(mark.tool == MarkTool::Ellipse)
    path.addEllipse(QRectF(start, end).normalized());
  else
  {
    path.moveTo(start);
    for(const QPointF &point : mark.points)
      path.lineTo(map(point));
    if(mark.tool == MarkTool::Arrow)
    {
      const QLineF line(start, end);
      if(line.length() > 0)
      {
        const double angle = std::atan2(end.y() - start.y(), end.x() - start.x());
        const double head = qMin(line.length() * 0.4, qMax(0.0125, mark.width * 4) * rect.width());
        for(double side : {-0.55, 0.55})
        {
          path.moveTo(end);
          path.lineTo(end - QPointF(std::cos(angle + side), std::sin(angle + side)) * head);
        }
      }
    }
  }
  return path;
}

void drawMarks(QPainter &painter, const QVector<Mark> &marks, const QRectF &rect)
{
  painter.setRenderHint(QPainter::Antialiasing);
  painter.setBrush(Qt::NoBrush);
  for(const Mark &mark : marks)
  {
    const QPainterPath path = markPath(mark, rect);
    const double width = qMax(1.0, mark.width * rect.width());
    if(path.isEmpty() && !mark.points.isEmpty())
    {
      // A click is a brush dab; Qt does not stroke a zero-length QPainterPath.
      const QPointF uv = mark.points.first();
      const QPointF center(rect.left() + uv.x() * rect.width(), rect.top() + uv.y() * rect.height());
      painter.setPen(Qt::NoPen);
      if(mark.tool == MarkTool::Eraser)
      {
        painter.setCompositionMode(QPainter::CompositionMode_Clear);
        painter.setBrush(Qt::transparent);
      }
      else
      {
        painter.setBrush(QColor(0, 0, 0, 190));
        painter.drawEllipse(center, width / 2 + 1, width / 2 + 1);
        painter.setBrush(mark.color);
      }
      painter.drawEllipse(center, width / 2, width / 2);
      painter.setCompositionMode(QPainter::CompositionMode_SourceOver);
      painter.setBrush(Qt::NoBrush);
      continue;
    }
    if(mark.tool == MarkTool::Eraser)
    {
      // Called only on a transparent overlay: never clear captured RT pixels.
      painter.setCompositionMode(QPainter::CompositionMode_Clear);
      painter.setPen(QPen(Qt::transparent, width, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
      painter.drawPath(path);
      painter.setCompositionMode(QPainter::CompositionMode_SourceOver);
      continue;
    }
    // A dark outline keeps marks legible against both bright and dark targets.
    painter.setPen(QPen(QColor(0, 0, 0, 190), width + 2, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    painter.drawPath(path);
    painter.setPen(QPen(mark.color, width, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    painter.drawPath(path);
  }
}
}

class AIAnnotationCanvas : public QWidget
{
public:
  explicit AIAnnotationCanvas(const QImage &image, QWidget *parent) : QWidget(parent), image(image)
  {
    setMinimumSize(120, 48);
    setCursor(Qt::CrossCursor);
    setFocusPolicy(Qt::StrongFocus);
    setMouseTracking(true);
    // i18n-ignore: stable QObject identifier used by native UI tests.
    setObjectName(QLatin1String("aiAnnotationCanvas"));
  }
  MarkTool tool = MarkTool::Pen;
  QColor color = QColor(255, 64, 80);
  int penWidth = 3;
  int eraserWidth = 24;
  std::function<void()> changed;
  const QImage image;
  QVector<Mark> marks;
  QVector<QVector<Mark>> undo, redo;

  void loadStrokes(const QJsonArray &strokes)
  {
    QVector<Mark> restored;
    int points = 0;
    if(strokes.size() > 64)
      return;
    for(const QJsonValue &value : strokes)
    {
      const QJsonObject stroke = value.toObject();
      const QString strokeTool = stroke.value(QLatin1String("tool")).toString();
      Mark mark;
      if(strokeTool == QLatin1String("pen"))
        mark.tool = MarkTool::Pen;
      else if(strokeTool == QLatin1String("ellipse"))
        mark.tool = MarkTool::Ellipse;
      else if(strokeTool == QLatin1String("arrow"))
        mark.tool = MarkTool::Arrow;
      else if(strokeTool == QLatin1String("eraser"))
        mark.tool = MarkTool::Eraser;
      else
        return;
      mark.color = QColor(stroke.value(QLatin1String("color")).toString());
      mark.width = stroke.value(QLatin1String("width")).toDouble();
      const QJsonArray vertices = stroke.value(QLatin1String("points")).toArray();
      if(!mark.color.isValid() || !std::isfinite(mark.width) || mark.width <= 0 ||
         mark.width > (mark.tool == MarkTool::Eraser ? 1.0 : 0.1) || vertices.size() < 2 ||
         vertices.size() > 256 ||
         ((mark.tool == MarkTool::Ellipse || mark.tool == MarkTool::Arrow) && vertices.size() != 2))
        return;
      for(const QJsonValue &vertex : vertices)
      {
        const QJsonArray xy = vertex.toArray();
        if(xy.size() != 2 || !xy[0].isDouble() || !xy[1].isDouble())
          return;
        const double x = xy[0].toDouble(), y = xy[1].toDouble();
        if(!std::isfinite(x) || !std::isfinite(y) || x < 0 || x > 1 || y < 0 || y > 1)
          return;
        mark.points.push_back(QPointF(x, y));
      }
      points += mark.points.size();
      if(points > 1024)
        return;
      restored.push_back(mark);
    }
    marks = restored;
    update();
  }

  void refreshCursor()
  {
    setCursor(tool == MarkTool::Eraser ? Qt::BlankCursor : Qt::CrossCursor);
    update();
  }

  void fit()
  {
    fitted = true;
    update();
  }

  void setImageViewRect(const QRectF &rect)
  {
    if(!rect.isValid())
      return;
    // Keep the image in panel coordinates: wrapping the toolbar or resizing the canvas
    // must not change the inherited pixel scale or move the image with the toolbar.
    viewRect = rect.translated(pos());
    fitted = false;
    update();
  }

  void restore(bool backwards)
  {
    auto &from = backwards ? undo : redo;
    auto &to = backwards ? redo : undo;
    if(from.isEmpty() || drawing)
      return;
    to.push_back(marks);
    marks = from.takeLast();
    update();
    changed();
  }
  void clear()
  {
    if(drawing || marks.isEmpty())
      return;
    remember();
    marks.clear();
    update();
    changed();
  }
  QImage annotated() const
  {
    QImage result = image.convertToFormat(QImage::Format_RGB32);
    QImage overlay(image.size(), QImage::Format_ARGB32_Premultiplied);
    overlay.fill(Qt::transparent);
    QPainter ink(&overlay);
    drawMarks(ink, marks, QRectF(QPointF(), image.size()));
    ink.end();
    QPainter painter(&result);
    painter.drawImage(0, 0, overlay);
    return result;
  }
  QJsonArray strokes() const
  {
    QJsonArray result;
    for(const Mark &mark : marks)
    {
      QJsonObject stroke;
      stroke[QLatin1String("tool")] = mark.tool == MarkTool::Ellipse  ? QLatin1String("ellipse")
                                      : mark.tool == MarkTool::Arrow  ? QLatin1String("arrow")
                                      : mark.tool == MarkTool::Eraser ? QLatin1String("eraser")
                                                                      : QLatin1String("pen");
      stroke[QLatin1String("color")] = mark.color.name();
      stroke[QLatin1String("width")] = mark.width;
      QJsonArray points;
      for(const QPointF &point : mark.points)
        points.append(QJsonArray(
            {std::round(point.x() * 100000) / 100000, std::round(point.y() * 100000) / 100000}));
      stroke[QLatin1String("points")] = points;
      result.append(stroke);
    }
    return result;
  }

protected:
  void paintEvent(QPaintEvent *) override
  {
    // i18n-ignore: native annotation API state, in canvas-local logical pixels.
    const QRectF bounds = imageRect();
    setProperty("annotationImageRect",
                QVariantList{bounds.x(), bounds.y(), bounds.width(), bounds.height()});
    setProperty("annotationStrokeCount", marks.size());
    setProperty("annotationSourceSize", image.size());
    QPainter painter(this);
    painter.fillRect(rect(), palette().brush(QPalette::Dark));
    painter.setRenderHint(QPainter::SmoothPixmapTransform,
                          bounds.width() * devicePixelRatioF() < image.width());
    painter.drawImage(bounds, image);
    QImage overlay(size(), QImage::Format_ARGB32_Premultiplied);
    overlay.fill(Qt::transparent);
    QPainter ink(&overlay);
    ink.setClipRect(imageRect());
    drawMarks(ink, marks, imageRect());
    if(drawing)
      drawMarks(ink, {current}, imageRect());
    ink.end();
    painter.drawImage(0, 0, overlay);
    if(tool == MarkTool::Eraser && underMouse())
    {
      const double diameter = qMin(double(eraserWidth), imageRect().width());
      painter.setBrush(Qt::NoBrush);
      painter.setPen(QPen(Qt::black, 3));
      painter.drawEllipse(cursorPoint, diameter / 2, diameter / 2);
      painter.setPen(QPen(Qt::white, 1));
      painter.drawEllipse(cursorPoint, diameter / 2, diameter / 2);
    }
  }
  void leaveEvent(QEvent *) override { update(); }
  void mousePressEvent(QMouseEvent *event) override
  {
    if(event->button() == Qt::MiddleButton && !drawing)
    {
      panning = true;
      lastPan = event->localPos();
      return;
    }
    if(event->button() != Qt::LeftButton || !imageRect().contains(event->localPos()))
      return;
    if(marks.size() >= 64)
      return;
    setFocus();
    remember();
    // Keep long pen gestures responsive within the bounded vector budget. Exact circle
    // corners and arrow tips are never resampled.
    while(pointCount() > 768)
      for(Mark &mark : marks)
        thinPen(mark);
    drawing = true;
    current = Mark();
    current.tool = tool;
    current.color = color;
    current.width = tool == MarkTool::Eraser ? qMin(1.0, eraserWidth / imageRect().width())
                                             : qMin(0.1, penWidth / imageRect().width());
    current.points.push_back(normalized(event->localPos()));
    current.points.push_back(current.points.first());
    cursorPoint = event->localPos();
    update();
  }
  void mouseMoveEvent(QMouseEvent *event) override
  {
    cursorPoint = event->localPos();
    if(tool == MarkTool::Eraser)
      update();
    if(panning)
    {
      setImageViewRect(imageRect().translated(event->localPos() - lastPan));
      lastPan = event->localPos();
      update();
      return;
    }
    if(!drawing)
      return;
    if(tool == MarkTool::Pen || tool == MarkTool::Eraser)
    {
      const QPointF point = normalized(event->localPos());
      if(current.points.size() >= 256 || pointCount() + current.points.size() >= 1024)
        thinPen(current);
      if(QLineF(current.points.last(), point).length() * imageRect().width() >= 2)
        current.points.push_back(point);
    }
    else
      current.points.last() = normalized(event->localPos());
    update();
  }
  void mouseReleaseEvent(QMouseEvent *event) override
  {
    if(event->button() == Qt::MiddleButton)
    {
      panning = false;
      return;
    }
    if(event->button() != Qt::LeftButton || !drawing)
      return;
    mouseMoveEvent(event);
    marks.push_back(current);
    drawing = false;
    update();
    changed();
  }
  void wheelEvent(QWheelEvent *event) override
  {
    if(drawing || event->angleDelta().y() == 0)
      return;
    const QRectF before = imageRect();
    const QPointF point = event->posF();
    const QPointF uv((point.x() - before.left()) / before.width(),
                     (point.y() - before.top()) / before.height());
    const double scale = before.width() / image.width();
    const double nextScale =
        qBound(0.001, scale * std::pow(1.25, event->angleDelta().y() / 120.0), 16384.0);
    const QSizeF size = before.size() * (nextScale / scale);
    setImageViewRect(QRectF(point - QPointF(uv.x() * size.width(), uv.y() * size.height()), size));
    event->accept();
  }

private:
  bool drawing = false;
  bool panning = false;
  bool fitted = true;
  QRectF viewRect;
  QPointF lastPan, cursorPoint;
  Mark current;
  QRectF imageRect() const
  {
    if(!fitted)
      return viewRect.translated(-pos());
    const QSizeF size = QSizeF(image.size()).scaled(QSizeF(this->size()), Qt::KeepAspectRatio);
    return QRectF(QPointF((width() - size.width()) / 2, (height() - size.height()) / 2), size);
  }
  static void thinPen(Mark &mark)
  {
    if((mark.tool != MarkTool::Pen && mark.tool != MarkTool::Eraser) || mark.points.size() <= 2)
      return;
    QVector<QPointF> sampled;
    for(int i = 0; i < mark.points.size() - 1; i += 2)
      sampled.push_back(mark.points[i]);
    sampled.push_back(mark.points.last());
    mark.points = sampled;
  }
  QPointF normalized(QPointF point) const
  {
    const QRectF area = imageRect();
    return QPointF(qBound(0.0, (point.x() - area.x()) / area.width(), 1.0),
                   qBound(0.0, (point.y() - area.y()) / area.height(), 1.0));
  }
  int pointCount() const
  {
    int count = 0;
    for(const Mark &mark : marks)
      count += mark.points.size();
    return count;
  }
  void remember()
  {
    undo.push_back(marks);
    if(undo.size() > 32)
      undo.removeFirst();
    redo.clear();
  }
};

AITextureAnnotation::AITextureAnnotation(const QImage &image, const QString &title,
                                         const QJsonObject &context, Accept accept,
                                         std::function<void()> close, QWidget *parent)
    : QWidget(parent)
{
  // i18n-ignore: stable QObject identifier used by native UI tests.
  setObjectName(QLatin1String("aiTextureAnnotation"));
  QVBoxLayout *layout = new QVBoxLayout(this);
  layout->setContentsMargins(4, 4, 4, 4);
  QLabel *identity = new QLabel(title, this);
  identity->setWordWrap(true);
  m_Header = new QGridLayout();
  m_Header->addWidget(identity, 0, 0);
  m_Toolbar = new QWidget(this);
  FlowLayout *tools = new FlowLayout(m_Toolbar, 0, 4, 3);
  QButtonGroup *group = new QButtonGroup(this);
  m_Canvas = new AIAnnotationCanvas(image, this);
  m_Canvas->loadStrokes(
      context.value(QLatin1String("annotation")).toObject().value(QLatin1String("strokes")).toArray());
  m_Canvas->setAccessibleName(tr("Render target annotation canvas"));
  const QString names[] = {tr("Pen"), tr("Circle"), tr("Arrow"), tr("Eraser")};
  for(int i = 0; i < 4; ++i)
  {
    QToolButton *button = new QToolButton(this);
    button->setText(names[i]);
    button->setCheckable(true);
    button->setChecked(i == 0);
    group->addButton(button, i);
    tools->addWidget(button);
    connect(button, &QToolButton::clicked, this, [this, i]() {
      m_Canvas->tool = MarkTool(i);
      m_Canvas->refreshCursor();
    });
    if(i == 3)
      button->setToolTip(
          tr("Erase only the marks inside the brush. The render target stays intact."));
  }
  QToolButton *color = new QToolButton(this);
  color->setText(tr("Color"));
  const auto updateColor = [this, color]() {
    QPixmap swatch(18, 18);
    swatch.fill(m_Canvas->color);
    color->setIcon(QIcon(swatch));
    color->setToolTip(tr("Mark color: %1").arg(m_Canvas->color.name()));
  };
  updateColor();
  tools->addWidget(color);
  connect(color, &QToolButton::clicked, this, [this, updateColor]() {
    const QColor selected = QColorDialog::getColor(m_Canvas->color, this, tr("Mark color"));
    if(selected.isValid())
      m_Canvas->color = selected;
    updateColor();
  });
  QSpinBox *width = new QSpinBox(this);
  width->setRange(1, 12);
  width->setValue(3);
  width->setToolTip(tr("Line width"));
  tools->addWidget(width);
  connect(width, static_cast<void (QSpinBox::*)(int)>(&QSpinBox::valueChanged), this,
          [this](int value) { m_Canvas->penWidth = value; });
  QWidget *eraserSettings = new QWidget(this);
  QHBoxLayout *eraserLayout = new QHBoxLayout(eraserSettings);
  eraserLayout->setContentsMargins(0, 0, 0, 0);
  eraserLayout->setSpacing(4);
  QLabel *eraserLabel = new QLabel(tr("Eraser width"), eraserSettings);
  QSpinBox *eraserWidth = new QSpinBox(eraserSettings);
  // i18n-ignore: stable QObject identifier used by native UI tests.
  eraserWidth->setObjectName(QLatin1String("aiAnnotationEraserWidth"));
  eraserWidth->setRange(4, 256);
  eraserWidth->setValue(m_Canvas->eraserWidth);
  eraserWidth->setSuffix(tr(" px"));
  eraserWidth->setToolTip(
      tr("Eraser diameter in view pixels; the circle previews the erased area."));
  eraserLabel->setBuddy(eraserWidth);
  eraserLayout->addWidget(eraserLabel);
  eraserLayout->addWidget(eraserWidth);
  tools->addWidget(eraserSettings);
  connect(eraserWidth, static_cast<void (QSpinBox::*)(int)>(&QSpinBox::valueChanged), this,
          [this](int value) {
            m_Canvas->eraserWidth = value;
            m_Canvas->update();
          });
  QToolButton *fit = new QToolButton(this);
  fit->setText(tr("Fit"));
  tools->addWidget(fit);
  connect(fit, &QToolButton::clicked, this, [this]() { m_Canvas->fit(); });
  m_Hint = new QLabel(
      tr("Mark the object or color region, then add it to the conversation and describe "
         "your question. Refer to marks by color. Only the marked image is shown in chat; "
         "the clean original is also sent to AI. Wheel to zoom; "
         "middle-drag to pan. Up to 64 marks."),
      this);
  m_Hint->setWordWrap(true);
  m_Canvas->setToolTip(m_Hint->text());
  QHBoxLayout *actions = new QHBoxLayout();
  QPushButton *undo = new QPushButton(tr("Undo"), this);
  QPushButton *redo = new QPushButton(tr("Redo"), this);
  QPushButton *clear = new QPushButton(tr("Clear"), this);
  QPushButton *cancel = new QPushButton(tr("Cancel"), this);
  const bool editing = !m_Canvas->marks.isEmpty();
  QPushButton *add =
      new QPushButton(editing ? tr("Update conversation marks") : tr("Add to conversation"), this);
  cancel->setMinimumSize(104, 36);
  add->setMinimumSize(148, 36);
  // i18n-ignore: action colors and states are Qt stylesheet rules.
  cancel->setStyleSheet(QLatin1String(
      "QPushButton { background-color:#8e3039; border:1px solid #a7545b; border-radius:4px; "
      "padding:4px 12px; color:#ffffff; }"
      "QPushButton:hover { background-color:#a53a44; }"
      "QPushButton:pressed { background-color:#742730; }"
      "QPushButton:focus { border-color:#ffd1d4; }"));
  // i18n-ignore: action colors and states are Qt stylesheet rules.
  add->setStyleSheet(QLatin1String(
      "QPushButton { background-color:#236443; border:1px solid #528e6d; border-radius:4px; "
      "padding:4px 12px; color:#ffffff; }"
      "QPushButton:hover:enabled { background-color:#2c7951; }"
      "QPushButton:pressed { background-color:#1c5137; }"
      "QPushButton:focus { border-color:#c3efd6; }"
      "QPushButton:disabled { background-color:#304b3e; border-color:#456354; color:#b5c8bd; }"));
  QWidget *history = new QWidget(this);
  QHBoxLayout *historyLayout = new QHBoxLayout(history);
  historyLayout->setContentsMargins(0, 0, 0, 0);
  historyLayout->setSpacing(4);
  for(QPushButton *button : {undo, redo, clear})
    historyLayout->addWidget(button);
  tools->addWidget(history);
  actions->addWidget(cancel);
  actions->addWidget(add);
  m_Header->addLayout(actions, 0, 1);
  m_Header->addWidget(m_Toolbar, 1, 0, 1, 2);
  m_Header->setColumnStretch(0, 1);
  layout->addLayout(m_Header);
  layout->addWidget(m_Canvas, 1);
  layout->addWidget(m_Hint);
  m_Canvas->changed = [this, undo, redo, clear, add]() {
    undo->setEnabled(!m_Canvas->undo.isEmpty());
    redo->setEnabled(!m_Canvas->redo.isEmpty());
    clear->setEnabled(!m_Canvas->marks.isEmpty());
    add->setEnabled(!m_Canvas->marks.isEmpty());
  };
  m_Canvas->changed();
  connect(undo, &QPushButton::clicked, this, [this]() { m_Canvas->restore(true); });
  connect(redo, &QPushButton::clicked, this, [this]() { m_Canvas->restore(false); });
  connect(clear, &QPushButton::clicked, this, [this]() { m_Canvas->clear(); });
  connect(cancel, &QPushButton::clicked, this, [close]() { close(); });
  connect(add, &QPushButton::clicked, this, [this, context, accept, close]() {
    QJsonObject metadata = context;
    QJsonObject annotation = metadata.value(QLatin1String("annotation")).toObject();
    annotation[QLatin1String("strokes")] = m_Canvas->strokes();
    metadata[QLatin1String("annotation")] = annotation;
    if(accept(m_Canvas->image, m_Canvas->annotated(), metadata))
      close();
  });
  QShortcut *undoKey = new QShortcut(QKeySequence::Undo, this);
  undoKey->setContext(Qt::WidgetWithChildrenShortcut);
  connect(undoKey, &QShortcut::activated, undo, &QPushButton::click);
  QShortcut *redoKey = new QShortcut(QKeySequence::Redo, this);
  redoKey->setContext(Qt::WidgetWithChildrenShortcut);
  connect(redoKey, &QShortcut::activated, redo, &QPushButton::click);
}

void AITextureAnnotation::SetImageViewRect(const QRectF &globalImageRect)
{
  m_Canvas->setImageViewRect(globalImageRect.translated(-m_Canvas->mapToGlobal(QPoint())));
}

void AITextureAnnotation::resizeEvent(QResizeEvent *event)
{
  QWidget::resizeEvent(event);
  if(!m_Hint)
    return;

  const QMargins margins = layout()->contentsMargins();
  const int width = qMax(1, event->size().width() - margins.left() - margins.right());
  QLayout *tools = m_Toolbar->layout();
  // FlowLayout needs the assigned width before it can report the height of wrapped rows.
  tools->setGeometry(QRect(0, 0, width, m_Toolbar->height()));
  m_Toolbar->setFixedHeight(tools->heightForWidth(width));
  const int headerHeight =
      qMax(m_Header->minimumSize().height(), m_Header->totalHeightForWidth(width));
  const int requiredHeight = headerHeight + m_Canvas->minimumHeight() +
                             m_Hint->heightForWidth(width) + margins.top() + margins.bottom() +
                             2 * layout()->spacing();
  m_Hint->setVisible(event->size().height() >= requiredHeight);
}
