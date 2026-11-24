#include "AIVisualReply.h"
#include "Windows/TexturePreviewChannels.h"
#include "AICodeFormatting.h"
#include "AIVisualFlowLayout.h"

#include <QApplication>
#include <QBuffer>
#include <QButtonGroup>
#include <QCheckBox>
#include <QColorDialog>
#include <QComboBox>
#include <QContextMenuEvent>
#include <QCryptographicHash>
#include <QDialog>
#include <QDir>
#include <QDoubleSpinBox>
#include <QFile>
#include <QFileInfo>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QHelpEvent>
#include <QImageReader>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLabel>
#include <QMenu>
#include <QMouseEvent>
#include <QPainter>
#include <QPainterPath>
#include <QPainterPathStroker>
#include <QPixmap>
#include <QPointer>
#include <QPushButton>
#include <QRegularExpression>
#include <QScrollBar>
#include <QSet>
#include <QSlider>
#include <QSplitter>
#include <QStackedWidget>
#include <QStandardPaths>
#include <QTextBrowser>
#include <QTextDocument>
#include <QTextLayout>
#include <QTextOption>
#include <QToolButton>
#include <QToolTip>
#include <QUrlQuery>
#include <QVBoxLayout>
#include <QVector3D>
#include <QWheelEvent>
#include <QWidgetAction>
#include <algorithm>
#include <cmath>
#include <limits>

struct AIVisualReplyLayoutState
{
  struct Diagram
  {
    QVector<QPointF> offsets;
    QVector<QVector<QPointF>> reroutes;
    QVector<QPointer<QWidget>> views;
    int revision = 0, resetRevision = 0;

    void changed()
    {
      ++revision;
      for(const auto &view : views)
        if(view)
          view->update();
    }
  };
  QHash<int, std::shared_ptr<Diagram>> diagrams;
};

namespace
{
QByteArray readVerified(const QString &path, const QString &hash, qint64 maximum)
{
  QFileInfo info(path);
  const QString root = QFileInfo(AIVisualReply::storageRoot()).canonicalFilePath();
  if(root.isEmpty() || !info.isFile() || info.isSymLink() || info.size() > maximum ||
     !info.canonicalFilePath().startsWith(root + QLatin1Char('/'), Qt::CaseInsensitive))
    return {};
  QFile file(path);
  if(!file.open(QIODevice::ReadOnly))
    return {};
  const QByteArray data = file.read(maximum + 1);
  if(data.size() > maximum ||
     QString::fromLatin1(QCryptographicHash::hash(data, QCryptographicHash::Sha256).toHex()) != hash)
    return {};
  return data;
}

QByteArray blob(const QJsonObject &asset, const QString &suffix)
{
  const QString hash = asset.value(QLatin1String("sha256")).toString();
  if(!AIVisualReply::validIdentity(hash) ||
     asset.value(QLatin1String("file")).toString() != hash + suffix)
    return {};
  return readVerified(AIVisualReply::storageRoot() + QLatin1String("/blobs/") + hash + suffix, hash,
                      8 * 1024 * 1024);
}

QImage imageAsset(const QJsonObject &asset, bool preserveAlpha = false)
{
  const QByteArray bytes = blob(asset, QLatin1String(".png"));
  QImage image;
  if(bytes.isEmpty())
    return image;
  QBuffer buffer;
  buffer.setData(bytes);
  buffer.open(QIODevice::ReadOnly);
  QImageReader reader(&buffer, "PNG");
  const QSize size = reader.size();
  if(size.width() <= 0 || size.height() <= 0 || size.width() > 8192 || size.height() > 8192 ||
     qint64(size.width()) * size.height() > 16000000)
    return image;
  const QSize preview = size.scaled(QSize(1600, 1600), Qt::KeepAspectRatio).boundedTo(size);
  if(!preserveAlpha)
    reader.setScaledSize(preview);
  image = reader.read().convertToFormat(preserveAlpha ? QImage::Format_ARGB32 : QImage::Format_RGB32);
  if(preserveAlpha && !image.isNull() && image.size() != preview)
    image = image.scaled(preview, Qt::IgnoreAspectRatio, Qt::FastTransformation);
  return image;
}

QPointF uvPoint(const QJsonValue &value, const QRectF &area)
{
  const QJsonArray point = value.toArray();
  if(point.size() != 2)
    return area.topLeft();
  return QPointF(area.left() + qBound(0.0, point[0].toDouble(), 1.0) * area.width(),
                 area.top() + qBound(0.0, point[1].toDouble(), 1.0) * area.height());
}

QString presentationTitle(QString text)
{
  const QStringList statuses = {
      QCoreApplication::translate("AIVisualReply", "Restored"),
      QCoreApplication::translate("AIVisualReply", "Reused existing reconstructed source")};
  for(const QString &status : statuses)
    text.remove(QRegularExpression(QStringLiteral("\\s*[\\x{ff08}(]\\s*%1\\s*[\\x{ff09})]")
                                       .arg(QRegularExpression::escape(status))));
  return text.trimmed();
}

QJsonValue visibleContent(const QJsonValue &value)
{
  if(value.isObject())
  {
    QJsonObject object = value.toObject();
    // A corrected jump target updates a presentation; it does not create a
    // second copy of the same pictures and explanation in one message.
    object.remove(QLatin1String("url"));
    for(auto it = object.begin(); it != object.end(); ++it)
      it.value() = visibleContent(it.value());
    return object;
  }
  if(value.isArray())
  {
    QJsonArray array;
    for(const QJsonValue &item : value.toArray())
      array.append(visibleContent(item));
    return array;
  }
  return value;
}

void imageText(QPainter &p, const QRectF &area, const QString &text, const QColor &color,
               Qt::Alignment alignment = Qt::AlignLeft | Qt::AlignTop)
{
  p.save();
  QFont font = p.font();
  font.setPointSizeF(qMax(12.0, font.pointSizeF() + 2.0));
  font.setBold(true);
  p.setFont(font);
  p.setPen(QColor(0, 0, 0, 210));
  for(const QPointF &offset : {QPointF(-1, 0), QPointF(1, 0), QPointF(0, -1), QPointF(0, 1)})
    p.drawText(area.translated(offset), alignment | Qt::TextWordWrap, text);
  p.setPen(color);
  p.drawText(area, alignment | Qt::TextWordWrap, text);
  p.restore();
}

void marks(QPainter &p, const QJsonArray &annotations, const QRectF &area, bool flip = false)
{
  p.save();
  p.setClipRect(area, Qt::IntersectClip);
  p.setRenderHint(QPainter::Antialiasing);
  auto mapPoint = [&](const QJsonValue &value) {
    QPointF point = uvPoint(value, area);
    if(flip)
      point.setY(area.top() + area.bottom() - point.y());
    return point;
  };
  for(int index = 0; index < qMin(32, annotations.size()); ++index)
  {
    const QJsonObject mark = annotations[index].toObject();
    const QJsonArray points = mark.value(QLatin1String("points")).toArray();
    if(points.isEmpty() || points.size() > 128)
      continue;
    QColor color(mark.value(QLatin1String("color")).toString());
    if(!color.isValid())
      color = Qt::red;
    const QString kind = mark.value(QLatin1String("type")).toString();
    const QPointF a = mapPoint(points.first()), b = mapPoint(points.last());
    QPainterPath path;
    if(kind == QLatin1String("ellipse"))
      path.addEllipse(QRectF(a, b).normalized());
    else if(kind == QLatin1String("rect"))
      path.addRect(QRectF(a, b).normalized());
    else
    {
      path.moveTo(a);
      for(const QJsonValue &point : points)
        path.lineTo(mapPoint(point));
      if(kind == QLatin1String("arrow"))
      {
        const double angle = std::atan2(b.y() - a.y(), b.x() - a.x());
        for(double side : {-0.55, 0.55})
        {
          path.moveTo(b);
          path.lineTo(b - QPointF(std::cos(angle + side), std::sin(angle + side)) * 12);
        }
      }
    }
    p.setBrush(Qt::NoBrush);
    p.setPen(QPen(Qt::black, 5));
    p.drawPath(path);
    p.setPen(QPen(color, 3));
    p.drawPath(path);
    const QString text = mark.value(QLatin1String("text")).toString().left(160);
    if(!text.isEmpty())
    {
      QFont font = p.font();
      font.setPointSizeF(qMax(12.0, font.pointSizeF() + 2.0));
      font.setBold(true);
      const QFontMetrics metrics(font);
      QRectF label = metrics
                         .boundingRect(QRect(0, 0, int(qMin(320.0, area.width() - 8)), 90),
                                       Qt::TextWordWrap, text)
                         .adjusted(-3, -2, 3, 2);
      label.moveTopLeft(a + QPointF(4, 4));
      label.moveLeft(
          qBound(area.left(), label.left(), qMax(area.left(), area.right() - label.width())));
      label.moveTop(qBound(area.top(), label.top(), qMax(area.top(), area.bottom() - label.height())));
      imageText(p, label.adjusted(3, 2, -3, -2), text, color);
    }
  }
  p.restore();
}

QUrl visualResourceLink(QUrl url, uint32_t eid)
{
  if(eid && url.scheme() == QLatin1String("rdoc") &&
     (url.host() == QLatin1String("resource") || url.host() == QLatin1String("texture")))
  {
    QUrlQuery query(url);
    if(!query.hasQueryItem(QLatin1String("eid")))
      query.addQueryItem(QLatin1String("eid"), QString::number(eid));
    url.setQuery(query);
  }
  return url;
}

const QRegularExpression &explanationLinkPattern()
{
  static const QRegularExpression pattern(
      QStringLiteral("\\[([^\\]\\n]+)\\]\\((rdoc://[^\\s<>()]+)\\)"));
  return pattern;
}

QString explanationHtml(const QString &text, uint32_t eid = 0)
{
  auto matches = explanationLinkPattern().globalMatch(text);
  QString html;
  int position = 0;
  while(matches.hasNext())
  {
    const auto match = matches.next();
    html += text.mid(position, match.capturedStart() - position).toHtmlEscaped();
    const QUrl target = visualResourceLink(QUrl(match.captured(2)), eid);
    const QStringList hosts = {
        QLatin1String("event"),   QLatin1String("eid"),     QLatin1String("resource"),
        QLatin1String("texture"), QLatin1String("buffer"),  QLatin1String("shader"),
        QLatin1String("mesh"),    QLatin1String("cbuffer"), QLatin1String("uv")};
    if(target.scheme() == QLatin1String("rdoc") && hosts.contains(target.host()))
      html += QLatin1String("<a href=\"") + target.toString(QUrl::FullyEncoded).toHtmlEscaped() +
              QLatin1String("\">") + match.captured(1).toHtmlEscaped() + QLatin1String("</a>");
    else
      html += match.captured(1).toHtmlEscaped();
    position = match.capturedEnd();
  }
  html += text.mid(position).toHtmlEscaped();
  return html.replace(QLatin1Char('\n'), QLatin1String("<br/>"));
}

QString codeHtml(const QJsonObject &code, const QPalette &palette)
{
  const bool example = code.value(QLatin1String("example")).toBool();
  QString html = QLatin1String(
      "<table width=\"100%\" cellpadding=\"3\" style=\"font-family:Consolas,monospace;\">");
  const QStringList lines =
      code.value(QLatin1String("text")).toString().left(16000).split(QLatin1Char('\n'));
  bool blockComment = false;
  const QJsonArray lineNumbers = code.value(QLatin1String("lineNumbers")).toArray();
  const QJsonArray mappings = code.value(QLatin1String("mappings")).toArray();
  const QJsonArray exactLines = code.value(QLatin1String("exactSourceLines")).toArray();
  QHash<int, QString> notes;
  for(const auto &value : code.value(QLatin1String("notes")).toArray())
  {
    const auto note = value.toObject();
    const int line = note.value(QLatin1String("sourceLine")).toInt();
    const QString text = note.value(QLatin1String("text")).toString().left(240).simplified();
    if(line > 0 && !text.isEmpty())
      notes.insert(line, text);
  }
  for(int i = 0; i < qMin(120, lines.size()); ++i)
  {
    QUrl url(code.value(QLatin1String("url")).toString());
    QUrlQuery query(url);
    const int startLine = code.value(QLatin1String("startLine")).toInt(1);
    int actual = i < lineNumbers.size() ? lineNumbers[i].toInt()
                 : example              ? startLine
                                        : startLine + i;
    if(actual <= 0 && example)
    {
      // Older examples recorded token targets without a row target. Recover
      // only a single agreed location; never guess across different sources.
      QSet<int> sources;
      for(const auto &entry : mappings)
      {
        const auto mapping = entry.toObject();
        if(mapping.value(QLatin1String("displayLine")).toInt() == i + 1 &&
           mapping.value(QLatin1String("sourceLine")).toInt() > 0)
          sources.insert(mapping.value(QLatin1String("sourceLine")).toInt());
      }
      if(sources.size() == 1)
        actual = *sources.constBegin();
    }
    const QString number = actual > 0 ? QString::number(actual) : QLatin1String("...");
    query.removeAllQueryItems(QLatin1String("line"));
    query.addQueryItem(QLatin1String("line"), QString::number(qMax(1, actual)));
    query.addQueryItem(QLatin1String("selectLine"), QLatin1String("1"));
    url.setQuery(query);
    const QString href = actual > 0 ? url.toString(QUrl::FullyEncoded).toHtmlEscaped() : QString();
    const QString note = notes.take(actual);
    if(!note.isEmpty())
    {
      bool noteComment = false;
      // i18n-ignore: HTML structure for the unlinked explanation row.
      html += QLatin1String("<tr><td></td><td>") +
              AICodeFormatting::highlightedSourceLine(QLatin1String("// ") + note,
                                                      QLatin1String("hlsl"), noteComment, palette) +
              QLatin1String("</td></tr>");
    }
    QHash<QString, QString> symbols;
    for(const auto &mappingValue : mappings)
    {
      const auto mapping = mappingValue.toObject();
      if(mapping.value(QLatin1String("displayLine")).toInt() != i + 1 ||
         mapping.value(QLatin1String("symbol")).toString().isEmpty())
        continue;
      QUrl jump(code.value(QLatin1String("url")).toString());
      QUrlQuery target(jump);
      target.removeAllQueryItems(QLatin1String("line"));
      target.removeAllQueryItems(QLatin1String("column"));
      target.addQueryItem(QLatin1String("line"),
                          QString::number(mapping.value(QLatin1String("sourceLine")).toInt()));
      target.addQueryItem(QLatin1String("column"),
                          QString::number(mapping.value(QLatin1String("column")).toInt(1)));
      target.addQueryItem(QLatin1String("symbol"),
                          mapping.value(QLatin1String("sourceSymbol")).toString());
      jump.setQuery(target);
      symbols.insert(mapping.value(QLatin1String("symbol")).toString(),
                     jump.toString(QUrl::FullyEncoded).toHtmlEscaped());
    }
    auto linked = [](const QString &text, const QString &target) {
      return target.isEmpty() ? text
                              : QLatin1String("<a style=\"text-decoration:none;\" href=\"") +
                                    target + QLatin1String("\">") + text + QLatin1String("</a>");
    };
    const QString rendered = AICodeFormatting::highlightedSourceLine(
        lines[i], code.value(QLatin1String("language")).toString(), blockComment, palette);
    const QRegularExpression token(QStringLiteral("<span\\b[^>]*>([^<]*)</span>"));
    auto matches = token.globalMatch(rendered);
    int position = 0;
    QString row;
    while(matches.hasNext())
    {
      const auto match = matches.next();
      row += linked(rendered.mid(position, match.capturedStart() - position), href);
      const QString word = match.captured(1);
      QString target = symbols.value(word, href);
      if((!example || (i < exactLines.size() && exactLines[i].toBool())) && actual > 0 &&
         !symbols.contains(word) &&
         (match.captured().contains(QLatin1String("source-variable")) ||
          match.captured().contains(QLatin1String("source-function"))))
      {
        QUrl jump = url;
        QUrlQuery location(jump);
        location.removeAllQueryItems(QLatin1String("column"));
        location.removeAllQueryItems(QLatin1String("selectLine"));
        location.addQueryItem(QLatin1String("column"), QString::number(lines[i].indexOf(word) + 1));
        location.addQueryItem(QLatin1String("symbol"), word);
        jump.setQuery(location);
        target = jump.toString(QUrl::FullyEncoded).toHtmlEscaped();
      }
      row += linked(match.captured(), target);
      position = match.capturedEnd();
    }
    row += linked(rendered.mid(position), href);
    html += QLatin1String("<tr><td width=\"40\">") + linked(number, href) +
            QLatin1String("</td><td style=\"white-space:pre;\">") + row +
            QLatin1String("</td></tr>");
  }
  return html + QLatin1String("</table>");
}

QString calculationHtml(const QString &calculation, const QPalette &palette)
{
  // Compact wrapped statements from older explanations while preserving statement boundaries.
  static const QRegularExpression continuationStart(QStringLiteral(R"(^[+*%&|?:,)]|^[-/]\s)"));
  static const QRegularExpression continuationEnd(QStringLiteral(R"([,(+*/%&|?:=-]$)"));
  static const QRegularExpression codeStatement(
      QStringLiteral(R"(^[A-Za-z_][\x20-\x7e]*[A-Za-z0-9_)\]]$)"));
  static const QRegularExpression commentOrString(
      QStringLiteral(R"rx("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|//|/\*)rx"));
  QStringList statements;
  bool inBlock = false;
  for(const QString &line : calculation.split(QLatin1Char('\n')))
  {
    const QString part = line.trimmed();
    const bool wasInBlock = inBlock;
    AICodeFormatting::lexicalSourceLine(part, QLatin1String("hlsl"), inBlock);
    if(!part.isEmpty() && !wasInBlock && !inBlock && !part.startsWith(QLatin1String("//")) &&
       !part.startsWith(QLatin1String("/*")) && !statements.isEmpty() &&
       !statements.last().contains(QLatin1String("//")) &&
       !statements.last().contains(QLatin1String("*/")) &&
       (continuationStart.match(part).hasMatch() ||
        continuationEnd.match(statements.last()).hasMatch()))
      statements.last() += QLatin1Char(' ') + part;
    else
      statements.push_back(part);
  }
  QString html = QLatin1String("<pre style=\"font-family:Consolas,monospace;\">");
  bool blockComment = false;
  for(int i = 0; i < statements.size(); ++i)
  {
    if(i)
      html += QLatin1String("<br/>");
    QString statement = statements[i];
    if(!blockComment)
    {
      auto tokens = commentOrString.globalMatch(statement);
      int commentStart = statement.size();
      while(tokens.hasNext())
      {
        const auto token = tokens.next();
        if(token.captured().startsWith(QLatin1Char('/')))
        {
          commentStart = token.capturedStart();
          break;
        }
      }
      QString expression = statement.left(commentStart).trimmed();
      const QString comment = statement.mid(commentStart);
      // Normalize old mathematical punctuation only in the expression. Leave
      // comments intact, and insert a missing semicolon before their marker.
      expression.replace(QChar(0x00b7), QLatin1String(" * "));
      expression.replace(QChar(0x00d7), QLatin1String(" * "));
      expression.replace(QChar(0x2212), QLatin1Char('-'));
      if((expression.contains(QLatin1Char('=')) || expression.contains(QLatin1Char('('))) &&
         codeStatement.match(expression).hasMatch())
        expression += QLatin1Char(';');
      if(!expression.isEmpty() && !comment.isEmpty())
        expression += QLatin1Char(' ');
      statement = expression + comment;
    }
    html += AICodeFormatting::highlightedSourceLine(statement, QLatin1String("hlsl"), blockComment,
                                                    palette);
  }
  return html + QLatin1String("</pre>");
}

class VisualCanvas : public QWidget
{
public:
  static constexpr double DiagramNodeWidth = 310.0;
  static constexpr int DifferenceMode = 4;
  struct DiagramChannels
  {
    unsigned mask = 7;
    qint64 sourceKey = 0;
    QImage display;
  };
  QJsonObject card;
  QImage first, second;
  QImage difference;
  bool identicalPair = false;
  QVector<QImage> nodeImages, nodeSecondImages;
  QVector<DiagramChannels> nodeChannels;
  int mode = 2;
  double ratio = .5, zoom = 1;
  QRectF focus;
  QPointF pan, last;
  bool dragging = false;
  bool flipY = false;
  int selectedNode = -1, selectedEdge = -1;
  std::function<void(int)> ratioChanged;
  std::function<void(int)> modeChanged;
  std::function<void(uint32_t)> nodeClicked;
  std::function<void(const QUrl &)> linkClicked;
  std::function<void(int, int)> diagramSelected;
  using DiagramEdits = AIVisualReplyLayoutState::Diagram;
  VisualCanvas(const QJsonObject &description, QWidget *parent,
               std::shared_ptr<DiagramEdits> edits = {})
      : QWidget(parent), card(description), diagramEdits(std::move(edits))
  {
    // i18n-ignore: stable native canvas identifier.
    setObjectName(QLatin1String("aiVisualImageCanvas"));
    setMinimumSize(100, 160);
    setMouseTracking(true);
    first = imageAsset(card.value(QLatin1String("image")).toObject());
    second = imageAsset(card.value(QLatin1String("secondImage")).toObject());
    // Old reference manifests recorded an explicit orientation without applying it.
    auto orientLegacyReference = [](QImage &img, const QJsonObject &asset) {
      if(!asset.contains(QLatin1String("presentationFlipApplied")) &&
         asset.value(QLatin1String("provenance")).toString() == QLatin1String("reference") &&
         asset.value(QLatin1String("source")).toObject().value(QLatin1String("flipY")).toBool())
        img = img.mirrored(false, true);
    };
    orientLegacyReference(first, card.value(QLatin1String("image")).toObject());
    orientLegacyReference(second, card.value(QLatin1String("secondImage")).toObject());
    if(card.value(QLatin1String("type")).toString() == QLatin1String("compare") &&
       !second.isNull() && first == second)
    {
      identicalPair = true;
      second = QImage();
      mode = 0;
    }
    setProperty("visualCompareMode", mode);
    const QJsonArray nodes = card.value(QLatin1String("nodes")).toArray();
    for(int i = 0; i < qMin(12, nodes.size()); ++i)
    {
      const QJsonObject node = nodes[i].toObject();
      nodeImages.push_back(imageAsset(node.value(QLatin1String("image")).toObject(), true));
      nodeSecondImages.push_back(
          imageAsset(node.value(QLatin1String("secondImage")).toObject(), true));
    }
    nodeChannels.resize(nodeImages.size() * 2);
    if(card.value(QLatin1String("type")).toString() == QLatin1String("diagram"))
    {
      if(!diagramEdits)
        diagramEdits = std::make_shared<DiagramEdits>();
      const int edgeCount = qMin(24, card.value(QLatin1String("edges")).toArray().size());
      diagramEdits->offsets.resize(nodeImages.size());
      diagramEdits->reroutes.resize(edgeCount);
      auto &views = diagramEdits->views;
      views.erase(std::remove_if(views.begin(), views.end(),
                                 [](const QPointer<QWidget> &view) { return view.isNull(); }),
                  views.end());
      views.push_back(this);
    }
    setProperty("visualWipeRatio", ratio);
  }
  bool hasAlignedPair() const
  {
    return !first.isNull() && !second.isNull() && first.size() == second.size();
  }
  void applyComparisonMode(int preferred)
  {
    mode = preferred;
    if(second.isNull())
      mode = 0;
    else if((mode == 2 || mode == DifferenceMode) && !hasAlignedPair())
      mode = 3;
    setProperty("visualCompareMode", mode);
    if(modeChanged)
      modeChanged(mode);
    update();
  }
  const QImage &differenceImage()
  {
    if(!difference.isNull() || !hasAlignedPair())
      return difference;

    const QImage a = first.convertToFormat(QImage::Format_RGB32);
    const QImage b = second.convertToFormat(QImage::Format_RGB32);
    difference = QImage(a.size(), QImage::Format_Grayscale8);
    if(difference.isNull())
      return difference;
    // Fixed square-root response makes small changes visible across different pairs.
    uchar intensity[256];
    for(int i = 0; i < 256; ++i)
      intensity[i] = uchar(qRound(255.0 * std::sqrt(i / 255.0)));
    for(int y = 0; y < a.height(); ++y)
    {
      const QRgb *left = reinterpret_cast<const QRgb *>(a.constScanLine(y));
      const QRgb *right = reinterpret_cast<const QRgb *>(b.constScanLine(y));
      uchar *output = difference.scanLine(y);
      for(int x = 0; x < a.width(); ++x)
      {
        const int delta =
            std::max({qAbs(qRed(left[x]) - qRed(right[x])), qAbs(qGreen(left[x]) - qGreen(right[x])),
                      qAbs(qBlue(left[x]) - qBlue(right[x]))});
        output[x] = intensity[delta];
      }
    }
    return difference;
  }
  void comparisonLabels(QPainter &p)
  {
    if(card.value(QLatin1String("type")).toString() != QLatin1String("compare"))
      return;
    if(identicalPair)
    {
      imageText(p, QRectF(rect()).adjusted(8, 8, -8, -8),
                QCoreApplication::translate("AIVisualReply", "No visible difference"), Qt::white);
      return;
    }
    QString before = presentationTitle(card.value(QLatin1String("beforeLabel")).toString());
    QString after = presentationTitle(card.value(QLatin1String("afterLabel")).toString());
    if(before.isEmpty())
      before = QCoreApplication::translate("AIVisualReply", "Original");
    if(after.isEmpty())
      after = QCoreApplication::translate("AIVisualReply", "Modified");
    const QString a = QCoreApplication::translate("AIVisualReply", "A: %1").arg(before);
    const QString b = QCoreApplication::translate("AIVisualReply", "B: %1").arg(after);
    auto label = [&](const QString &text, const QRectF &bounds, bool right) {
      if(bounds.width() < 48)
        return;
      p.save();
      p.setClipRect(bounds);
      imageText(p, bounds.adjusted(8, 8, -8, -8), text, Qt::white,
                (right ? Qt::AlignRight : Qt::AlignLeft) | Qt::AlignTop);
      p.restore();
    };
    if(second.isNull() || mode == 0)
      label(a, rect(), false);
    else if(mode == 1)
      label(b, rect(), false);
    else
    {
      const qreal split = mode == 3 ? width() * .5 : width() * ratio;
      label(a, QRectF(0, 0, split, height()), false);
      label(b, QRectF(split, 0, width() - split, height()), true);
    }
  }
  QRectF area(const QRectF &bounds, const QImage &image) const
  {
    if(focus.isValid())
    {
      const double scale = qMin(bounds.width() / (image.width() * focus.width()),
                                bounds.height() / (image.height() * focus.height())) *
                           zoom;
      const QSizeF size(image.width() * scale, image.height() * scale);
      const QPointF center(focus.center().x(), flipY ? 1.0 - focus.center().y() : focus.center().y());
      return QRectF(
          bounds.center() - QPointF(center.x() * size.width(), center.y() * size.height()) + pan,
          size);
    }
    QSizeF size = QSizeF(image.size()).scaled(bounds.size(), Qt::KeepAspectRatio) * zoom;
    return QRectF(bounds.center() - QPointF(size.width() / 2, size.height() / 2) + pan, size);
  }
  void draw(QPainter &p, const QImage &image, const QJsonArray &ink, const QRectF &bounds)
  {
    if(image.isNull())
      return;
    const QRectF fitted = area(bounds, image);
    p.save();
    // Preserve the enclosing wipe/viewport clip. Replacing it lets A overwrite
    // the entire B image even though both source assets are different.
    p.setClipRect(bounds, Qt::IntersectClip);
    if(flipY)
    {
      p.save();
      p.translate(0, fitted.top() + fitted.bottom());
      p.scale(1, -1);
      p.drawImage(fitted, image);
      p.restore();
    }
    else
      p.drawImage(fitted, image);
    marks(p, ink, fitted, flipY);
    p.restore();
  }
  double diagramTextHeight(const QString &text, double width) const
  {
    if(text.isEmpty())
      return 0;
    QString lines = text;
    lines.replace(QLatin1Char('\n'), QChar(QChar::LineSeparator));
    QTextLayout layout(lines, font());
    QTextOption option(Qt::AlignHCenter);
    option.setWrapMode(QTextOption::WrapAtWordBoundaryOrAnywhere);
    layout.setTextOption(option);
    layout.beginLayout();
    double height = 0;
    while(true)
    {
      QTextLine line = layout.createLine();
      if(!line.isValid())
        break;
      line.setLineWidth(qMax(1.0, width));
      if(height > 0)
        height += qMax(0.0, line.leading());
      height += line.height();
    }
    layout.endLayout();
    return height;
  }
  void diagramText(QPainter &p, const QRectF &area, const QString &text) const
  {
    QTextOption option(Qt::AlignHCenter | Qt::AlignTop);
    option.setWrapMode(QTextOption::WrapAtWordBoundaryOrAnywhere);
    p.drawText(area, text, option);
  }
  QString diagramImageLabel(int index, int side, bool paired) const
  {
    const auto node = card.value(QLatin1String("nodes")).toArray()[index].toObject();
    QString label =
        node.value(side ? QLatin1String("secondImageLabel") : QLatin1String("imageLabel")).toString();
    // i18n-ignore: compact preview dimension identifiers.
    if(label.isEmpty() && paired)
      label = side ? QLatin1String("3D") : QLatin1String("2D");
    return label;
  }
  double diagramLabelsHeight(int index, double width) const
  {
    const bool paired = !nodeImages[index].isNull() && !nodeSecondImages[index].isNull();
    double height = 0;
    for(int side = 0; side < 2; ++side)
      if(!(side ? nodeSecondImages[index] : nodeImages[index]).isNull())
        height = qMax(height, diagramTextHeight(diagramImageLabel(index, side, paired),
                                                paired ? (width - 6) * .5 : width));
    return height > 0 ? height + 3 : 0;
  }
  double diagramMediaHeight(int index, double width) const
  {
    const QImage &field = nodeImages[index], &projection = nodeSecondImages[index];
    if(field.isNull() && projection.isNull())
      return 0;
    const bool paired = !field.isNull() && !projection.isNull();
    const double imageWidth = paired ? (width - 6) * .5 : width;
    double height = 0;
    for(const QImage *image : {&field, &projection})
      if(!image->isNull())
        height = qMax(height, qMin(180.0, imageWidth * image->height() / image->width()));
    return qMax(22.0, height) + diagramLabelsHeight(index, width) + 8;
  }
  QRectF diagramImageBounds(int index, int side, const QRectF &box) const
  {
    const QRectF content = box.adjusted(6, 5, -6, -5);
    const auto node = card.value(QLatin1String("nodes")).toArray()[index].toObject();
    const bool paired = !nodeImages[index].isNull() && !nodeSecondImages[index].isNull();
    const double width = paired ? (content.width() - 6) * .5 : content.width();
    const double titleHeight =
        diagramTextHeight(node.value(QLatin1String("text")).toString(), content.width());
    const double labelHeight = diagramLabelsHeight(index, content.width());
    return QRectF(content.left() + (paired ? side * (width + 6) : 0),
                  content.top() + titleHeight + 2 + labelHeight, width,
                  diagramMediaHeight(index, content.width()) - labelHeight - 8);
  }
  QRectF diagramChannelRect(const QRectF &bounds) const
  {
    return QRectF(bounds.right() - 47, bounds.bottom() - 21, 44, 18);
  }
  int diagramChannelAt(int index, const QPointF &position) const
  {
    const QRectF box = diagramBoxes()[index];
    const QPointF point = diagramTransform().inverted().map(position);
    for(int side = 0; side < 2; ++side)
      if(!(side ? nodeSecondImages[index] : nodeImages[index]).isNull() &&
         diagramChannelRect(diagramImageBounds(index, side, box)).contains(point))
        return side;
    return -1;
  }
  QString channelName(unsigned mask) const
  {
    QString name;
    for(int i = 0; i < 4; ++i)
      if(mask & (1U << i))
        name += QLatin1Char("RGBA"[i]);
    return name.isEmpty() ? QString(QChar(0x2014)) : name;
  }
  const QImage &diagramChannelImage(int index, int side)
  {
    const QImage &source = side ? nodeSecondImages[index] : nodeImages[index];
    DiagramChannels &channels = nodeChannels[index * 2 + side];
    if(channels.sourceKey != source.cacheKey())
    {
      channels.display = TexturePreviewChannels(source, channels.mask);
      channels.sourceKey = source.cacheKey();
    }
    return channels.display;
  }
  void showDiagramChannels(int index, int side, const QPoint &globalPosition)
  {
    const int imageIndex = index * 2 + side;
    QMenu *menu = new QMenu(this);
    menu->setObjectName(
        QLatin1String("aiDiagramChannelMenu"));    // i18n-ignore: native UI identifier.
    QWidgetAction *action = new QWidgetAction(menu);
    QWidget *options = new QWidget(menu);
    QVBoxLayout *layout = new QVBoxLayout(options);
    layout->setContentsMargins(8, 5, 8, 5);
    QVector<QCheckBox *> checks;
    for(unsigned mask : {15U, 1U, 2U, 4U, 8U})
    {
      QCheckBox *check = new QCheckBox(channelName(mask), options);
      check->setChecked((nodeChannels[imageIndex].mask & mask) == mask);
      checks.push_back(check);
      layout->addWidget(check);
    }
    for(int i = 0; i < checks.size(); ++i)
    {
      connect(checks[i], &QCheckBox::clicked, menu, [this, imageIndex, checks, i](bool checked) {
        DiagramChannels &channels = nodeChannels[imageIndex];
        const unsigned bits = i == 0 ? 15U : 1U << (i - 1);
        channels.mask = checked ? channels.mask | bits : channels.mask & ~bits;
        channels.sourceKey = 0;
        if(imageIndex / 2 < diagramCardContent.size())
          diagramCardContent[imageIndex / 2] = QPixmap();
        for(int j = 0; j < checks.size(); ++j)
        {
          const unsigned mask = j == 0 ? 15U : 1U << (j - 1);
          checks[j]->setChecked((channels.mask & mask) == mask);
        }
        update();
      });
    }
    action->setDefaultWidget(options);
    menu->addAction(action);
    connect(menu, &QMenu::aboutToHide, menu, &QObject::deleteLater);
    menu->popup(globalPosition);
  }
  void resetDiagramLayout()
  {
    diagramEdits->offsets.fill(QPointF());
    diagramEdits->reroutes.fill(QVector<QPointF>());
    diagramEdits->resetRevision = diagramEdits->revision + 1;
    diagramEdits->changed();
    zoom = 1;
    pan = QPointF();
  }

  const QVector<QRectF> &diagramBoxes() const
  {
    updateDiagramGeometry();
    return diagramCardBoxes;
  }

  QTransform diagramTransform() const
  {
    updateDiagramGeometry();
    const double fit = qBound(.01,
                              qMin((width() - 20) / qMax(1.0, diagramFrame.width()),
                                   (height() - 20) / qMax(1.0, diagramFrame.height())),
                              1.5);
    QTransform transform;
    transform.translate(width() * .5 + pan.x(), height() * .5 + pan.y());
    transform.scale(fit * zoom, fit * zoom);
    transform.translate(-diagramFrame.center().x(), -diagramFrame.center().y());
    return transform;
  }

  QPainterPath diagramWire(int index) const
  {
    diagramWires();
    return diagramPaths.value(index);
  }

  int diagramEdgeAt(const QPointF &position) const { return diagramWireAt(position).edge; }

private:
  enum class DiagramGesture
  {
    Pan,
    Node,
    Reroute,
  };
  struct DiagramHit
  {
    int edge = -1, leg = -1;
    QPointF point;
  };

  std::shared_ptr<DiagramEdits> diagramEdits;
  mutable QVector<QRectF> diagramAutoBoxes, diagramCardBoxes;
  mutable QRectF diagramFrame;
  mutable AIVisualFlow::Links diagramLinks;
  mutable QVector<AIVisualFlow::Wire> diagramRoutes;
  mutable QVector<QVector<QPainterPath>> diagramPathLegs;
  mutable QVector<QPainterPath> diagramPaths;
  mutable int diagramBoxRevision = -1, diagramRouteRevision = -1;
  DiagramGesture diagramGesture = DiagramGesture::Pan;
  Qt::MouseButton diagramButton = Qt::NoButton;
  QPointF diagramPress, diagramDragOrigin;
  int draggedNode = -1, draggedEdge = -1, draggedReroute = -1;
  bool diagramMoved = false;
  QVector<QPixmap> diagramCardContent;
  double diagramCardScale = 0;
  bool diagramCardFlipY = false;

  bool diagramDragging(DiagramGesture gesture) const
  {
    return diagramButton != Qt::NoButton && diagramMoved && diagramGesture == gesture;
  }

  QPointF diagramDragPosition(const QPointF &pointer) const
  {
    const QTransform inverse = diagramTransform().inverted();
    return diagramDragOrigin + inverse.map(pointer) - inverse.map(diagramPress);
  }

  void finishDiagramDrag(const QMouseEvent *release = nullptr)
  {
    const bool movingNode = diagramDragging(DiagramGesture::Node);
    diagramButton = Qt::NoButton;
    if(!movingNode)
      return;
    QPointF &offset = diagramEdits->offsets[draggedNode];
    const QPointF requested = release ? diagramDragPosition(release->localPos()) : offset;
    QVector<QRectF> boxes = diagramBoxes();
    boxes[draggedNode] = diagramAutoBoxes[draggedNode].translated(requested);
    const QPointF settled = requested + AIVisualFlow::NearestClearNodeDisplacement(
                                            boxes, draggedNode, diagramEdits->reroutes);
    if(offset != settled)
    {
      offset = settled;
      diagramEdits->changed();
    }
    update();
  }

  void updateDiagramGeometry() const
  {
    if(!diagramEdits || diagramBoxRevision == diagramEdits->revision)
      return;
    if(diagramAutoBoxes.isEmpty())
    {
      const auto nodes = card.value(QLatin1String("nodes")).toArray();
      if(nodes.isEmpty() || nodes.size() > 12)
        return;
      QHash<QString, int> ids;
      QVector<double> heights;
      const double contentWidth = DiagramNodeWidth - 12;
      for(int i = 0; i < nodes.size(); ++i)
      {
        const auto node = nodes[i].toObject();
        ids.insert(node.value(QLatin1String("id")).toString(), i);
        const QString title = node.value(QLatin1String("text")).toString();
        const QString note = node.value(QLatin1String("description")).toString().simplified();
        heights.push_back(12 + diagramTextHeight(title, contentWidth) +
                          diagramTextHeight(note, contentWidth) +
                          diagramMediaHeight(i, contentWidth));
      }
      const auto edges = card.value(QLatin1String("edges")).toArray();
      diagramLinks.clear();
      for(int i = 0; i < qMin(24, edges.size()); ++i)
      {
        const auto edge = edges[i].toArray();
        const int from = edge.size() == 2 ? ids.value(edge[0].toString(), -1) : -1;
        const int to = edge.size() == 2 ? ids.value(edge[1].toString(), -1) : -1;
        diagramLinks.push_back(from >= 0 && to >= 0 && from != to ? qMakePair(from, to)
                                                                  : qMakePair(-1, -1));
      }
      diagramAutoBoxes = AIVisualFlow::PlaceNodes(heights, diagramLinks, DiagramNodeWidth);
      diagramFrame = QRectF();
      for(const auto &box : diagramAutoBoxes)
        diagramFrame |= box;
      // Leave space for the outer lanes, then hold this framing while editing.
      if(!diagramLinks.isEmpty())
        diagramFrame.adjust(-36, -36, 36, 36);
    }
    diagramCardBoxes = diagramAutoBoxes;
    for(int i = 0; i < diagramCardBoxes.size(); ++i)
      diagramCardBoxes[i].translate(diagramEdits->offsets[i]);
    diagramBoxRevision = diagramEdits->revision;
  }

  const QVector<AIVisualFlow::Wire> &diagramWires() const
  {
    updateDiagramGeometry();
    if(diagramEdits && diagramBoxRevision >= 0 && diagramRouteRevision != diagramBoxRevision)
    {
      const auto previous = diagramRouteRevision >= diagramEdits->resetRevision
                                ? diagramRoutes
                                : QVector<AIVisualFlow::Wire>();
      diagramRoutes = AIVisualFlow::RouteWires(diagramCardBoxes, diagramLinks, diagramAutoBoxes,
                                               diagramEdits->reroutes, previous);
      diagramRouteRevision = diagramBoxRevision;
      updateDiagramPaths();
    }
    return diagramRoutes;
  }

  void updateDiagramPaths() const
  {
    diagramPathLegs.clear();
    diagramPathLegs.resize(diagramRoutes.size());
    diagramPaths.fill(QPainterPath(), diagramRoutes.size());
    for(int i = 0; i < diagramRoutes.size(); ++i)
    {
      for(const auto &leg : diagramRoutes[i].legs)
      {
        QPainterPath path(leg.curves.front().start);
        for(const auto &curve : leg.curves)
          path.cubicTo(curve.first, curve.second, curve.end);
        diagramPathLegs[i].push_back(path);
        diagramPaths[i].connectPath(path);
      }
    }
  }

  int diagramNodeAt(const QPointF &position) const
  {
    const QPointF point = diagramTransform().inverted().map(position);
    const auto &boxes = diagramBoxes();
    for(int i = 0; i < boxes.size(); ++i)
      if(boxes[i].contains(point))
        return i;
    return -1;
  }

  DiagramHit diagramWireAt(const QPointF &position) const
  {
    const auto transform = diagramTransform();
    const auto inverse = transform.inverted();
    diagramWires();
    double nearest = 7 * 7;
    DiagramHit hit;
    for(int i = 0; i < diagramPathLegs.size(); ++i)
    {
      for(int leg = 0; leg < diagramPathLegs[i].size(); ++leg)
      {
        for(const auto &points : transform.map(diagramPathLegs[i][leg]).toSubpathPolygons())
        {
          for(int j = 1; j < points.size(); ++j)
          {
            const QPointF start = points[j - 1], delta = points[j] - start;
            const double lengthSquared = QPointF::dotProduct(delta, delta);
            if(lengthSquared == 0)
              continue;
            const double t =
                qBound(0.0, QPointF::dotProduct(position - start, delta) / lengthSquared, 1.0);
            const QPointF projected = start + delta * t;
            const QPointF distance = position - projected;
            const double squared = QPointF::dotProduct(distance, distance);
            if(squared < nearest)
            {
              nearest = squared;
              hit = {i, leg, inverse.map(projected)};
            }
          }
        }
      }
    }
    return hit;
  }

  QPair<int, int> diagramRerouteAt(const QPointF &position) const
  {
    const auto transform = diagramTransform();
    const auto &reroutes = diagramEdits->reroutes;
    double nearest = 7 * 7;
    QPair<int, int> hit(-1, -1);
    for(int edge = 0; edge < reroutes.size(); ++edge)
    {
      for(int point = 0; point < reroutes[edge].size(); ++point)
      {
        const QPointF delta = position - transform.map(reroutes[edge][point]);
        const double squared = QPointF::dotProduct(delta, delta);
        if(squared < nearest)
        {
          nearest = squared;
          hit = qMakePair(edge, point);
        }
      }
    }
    return hit;
  }

  bool diagramCanPlaceReroute(const QPointF &position) const
  {
    for(const auto &box : diagramBoxes())
      if(AIVisualFlow::Margin(box).contains(position))
        return false;
    return true;
  }

  void selectDiagramItem(int node, int edge)
  {
    selectedNode = node;
    selectedEdge = edge;
    setProperty("visualSelectedNode", node);
    setProperty("visualSelectedEdge", edge);
    if(diagramSelected)
      diagramSelected(node, edge);
    update();
  }

  void addDiagramReroute(const QPointF &position)
  {
    const DiagramHit hit = diagramWireAt(position);
    if(hit.edge < 0 || !diagramCanPlaceReroute(hit.point))
      return;
    int count = 0;
    for(const auto &points : diagramEdits->reroutes)
      count += points.size();
    if(count >= 24 || diagramRerouteAt(position).first >= 0)
      return;
    diagramEdits->reroutes[hit.edge].insert(hit.leg, hit.point);
    diagramEdits->changed();
    selectDiagramItem(-1, hit.edge);
  }

  QColor diagramWireColor(int index) const
  {
    if(index != selectedEdge)
      return palette().color(QPalette::Highlight);
    return palette().color(QPalette::Dark).lightness() < 128 ? QColor(255, 170, 64)
                                                             : QColor(176, 84, 0);
  }

  void paintDiagramWires(QPainter &p)
  {
    const auto &wires = diagramWires();
    // Draw the selected wire last, so it stays legible at unavoidable crossings.
    for(bool selected : {false, true})
    {
      for(int i = 0; i < wires.size(); ++i)
      {
        const auto &wire = wires[i];
        if((i == selectedEdge) != selected || wire.legs.isEmpty())
          continue;
        const QColor color = diagramWireColor(i);
        p.setPen(QPen(color, selected ? 3 : 2, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
        p.drawPath(diagramPaths[i]);
        const auto &lastCurve = wire.legs.back().curves.back();
        const QPointF end = lastCurve.end, endDirection = end - lastCurve.second;
        const double angle = std::atan2(endDirection.y(), endDirection.x());
        for(double side : {-.5, .5})
          p.drawLine(end, end - QPointF(std::cos(angle + side), std::sin(angle + side)) * 8);

        p.save();
        p.setPen(QPen(color, 1.5));
        p.setBrush(palette().brush(QPalette::Dark));
        for(const auto &leg : wire.legs)
          for(int curve = 0; curve + 1 < leg.curves.size(); ++curve)
            p.drawEllipse(leg.curves[curve].end, 3.5, 3.5);
        p.setPen(QPen(palette().color(QPalette::Dark), 1.3));
        p.setBrush(color);
        for(const QPointF &handle : diagramEdits->reroutes[i])
          p.drawEllipse(handle, 5.5, 5.5);
        p.restore();
      }
    }
  }

  void paintDiagramCardContent(QPainter &p, const QJsonObject &node, int i, const QRectF &box)
  {
    p.setPen(palette().color(QPalette::Text));
    p.save();
    p.setClipRect(box.adjusted(4, 3, -4, -3));
    QRectF content = box.adjusted(6, 5, -6, -5);
    const QString title = node.value(QLatin1String("text")).toString();
    const double titleHeight = diagramTextHeight(title, content.width());
    diagramText(p, QRectF(content.topLeft(), QSizeF(content.width(), titleHeight)), title);
    content.setTop(content.top() + titleHeight + 2);
    if(!nodeImages[i].isNull() || !nodeSecondImages[i].isNull())
    {
      const bool paired = !nodeImages[i].isNull() && !nodeSecondImages[i].isNull();
      const double mediaHeight = diagramMediaHeight(i, content.width());
      const double labelHeight = diagramLabelsHeight(i, content.width());
      for(int side = 0; side < 2; ++side)
      {
        if((side ? nodeSecondImages[i] : nodeImages[i]).isNull())
          continue;
        const QImage &image = diagramChannelImage(i, side);
        const QRectF bounds = diagramImageBounds(i, side, box);
        const QSizeF size = QSizeF(image.size()).scaled(bounds.size(), Qt::KeepAspectRatio);
        const QRectF picture(bounds.center() - QPointF(size.width() / 2, size.height() / 2), size);
        diagramText(p, QRectF(bounds.left(), content.top(), bounds.width(), labelHeight),
                    diagramImageLabel(i, side, paired));
        p.drawImage(picture, flipY ? image.mirrored(false, true) : image);
        marks(p,
              node.value(side ? QLatin1String("secondAnnotations") : QLatin1String("annotations"))
                  .toArray(),
              picture, flipY);
        p.save();
        const QRectF channels = diagramChannelRect(bounds);
        p.setBrush(palette().brush(QPalette::Button));
        p.setPen(palette().color(QPalette::Mid));
        p.drawRoundedRect(channels, 3, 3);
        p.setPen(palette().color(QPalette::ButtonText));
        p.drawText(channels, Qt::AlignCenter, channelName(nodeChannels[i * 2 + side].mask));
        p.restore();
      }
      content.setTop(content.top() + mediaHeight);
    }
    const QString note = node.value(QLatin1String("description")).toString().simplified();
    const double noteHeight = diagramTextHeight(note, content.width());
    diagramText(p, QRectF(content.topLeft(), QSizeF(content.width(), noteHeight)), note);
    p.restore();
  }

protected:
  bool event(QEvent *event) override
  {
    if(event->type() == QEvent::Hide || event->type() == QEvent::UngrabMouse)
      finishDiagramDrag();
    if(event->type() == QEvent::FontChange && diagramEdits)
    {
      diagramAutoBoxes.clear();
      diagramBoxRevision = diagramRouteRevision = -1;
    }
    if(event->type() == QEvent::FontChange || event->type() == QEvent::PaletteChange)
      diagramCardContent.clear();
    if(event->type() == QEvent::ToolTip &&
       card.value(QLatin1String("type")).toString() == QLatin1String("diagram"))
    {
      if(diagramButton != Qt::NoButton)
        return true;
      QHelpEvent *help = static_cast<QHelpEvent *>(event);
      const int index = diagramNodeAt(help->pos());
      if(index >= 0)
      {
        if(diagramChannelAt(index, help->pos()) >= 0)
        {
          QToolTip::showText(
              help->globalPos(),
              QCoreApplication::translate(
                  "AIVisualReply",
                  "Choose display channels. A single channel is shown in grayscale."),
              this);
          return true;
        }
        const QJsonObject node = card.value(QLatin1String("nodes")).toArray()[index].toObject();
        QStringList details;
        // i18n-ignore: JSON protocol field names, not UI labels.
        for(const char *key : {"text", "description"})
        {
          const QString text = node.value(QLatin1String(key)).toString().trimmed();
          if(!text.isEmpty())
            details.push_back(text);
        }
        setToolTip(details.join(QLatin1Char('\n')));
        QToolTip::showText(help->globalPos(), toolTip(), this);
        return true;
      }
      setToolTip(QString());
      QToolTip::hideText();
      return true;
    }
    return QWidget::event(event);
  }
  void contextMenuEvent(QContextMenuEvent *event) override
  {
    if(card.value(QLatin1String("type")).toString() != QLatin1String("diagram"))
      return;
    const int index = diagramNodeAt(event->pos());
    QMenu menu(this);
    if(index < 0)
    {
      const auto reroute = diagramRerouteAt(event->pos());
      if(reroute.first >= 0)
      {
        QAction *remove =
            menu.addAction(QCoreApplication::translate("AIVisualReply", "Remove reroute point"));
        if(menu.exec(event->globalPos()) == remove &&
           reroute.second < diagramEdits->reroutes[reroute.first].size())
        {
          diagramEdits->reroutes[reroute.first].removeAt(reroute.second);
          diagramEdits->changed();
        }
      }
      return;
    }
    const QJsonObject node = card.value(QLatin1String("nodes")).toArray()[index].toObject();
    const uint32_t eid = uint32_t(node.value(QLatin1String("eventId")).toDouble());
    if(eid)
      connect(menu.addAction(QCoreApplication::translate("AIVisualReply", "EID %1").arg(eid)),
              &QAction::triggered, this, [this, eid]() {
                if(nodeClicked)
                  nodeClicked(eid);
              });
    for(const QJsonValue &value : node.value(QLatin1String("links")).toArray())
    {
      const QJsonObject link = value.toObject();
      const QUrl url =
          visualResourceLink(QUrl(link.value(QLatin1String("url")).toString()),
                             eid ? eid : uint32_t(card.value(QLatin1String("eventId")).toDouble()));
      if(url.scheme() == QLatin1String("rdoc"))
        connect(menu.addAction(link.value(QLatin1String("label")).toString()), &QAction::triggered,
                this, [this, url]() {
                  if(linkClicked)
                    linkClicked(url);
                });
    }
    if(!menu.isEmpty())
      menu.exec(event->globalPos());
  }
  void paintEvent(QPaintEvent *) override
  {
    QPainter p(this);
    p.fillRect(rect(), palette().color(QPalette::Dark));
    p.setRenderHint(QPainter::SmoothPixmapTransform);
    const QJsonArray nodes = card.value(QLatin1String("nodes")).toArray();
    if(card.value(QLatin1String("type")).toString() == QLatin1String("diagram"))
    {
      if(nodes.isEmpty() || nodes.size() > 12)
        return;
      const QTransform transform = diagramTransform();
      p.setTransform(transform);
      const auto &boxes = diagramBoxes();
      const QRectF viewport = transform.inverted().mapRect(rect());
      const bool movingNode = diagramDragging(DiagramGesture::Node);
      const double scale = transform.m11() * p.device()->devicePixelRatioF();
      if(diagramCardContent.isEmpty() || scale != diagramCardScale || flipY != diagramCardFlipY)
      {
        diagramCardContent.fill(QPixmap(), nodes.size());
        diagramCardScale = scale;
        diagramCardFlipY = flipY;
      }
      p.setRenderHint(QPainter::Antialiasing);
      paintDiagramWires(p);
      for(int order = 0; order < nodes.size(); ++order)
      {
        // The card under the pointer stays on top while crossing other cards.
        int i = order;
        if(movingNode && order >= draggedNode)
          i = order == nodes.size() - 1 ? draggedNode : order + 1;
        const QRectF &box = boxes[i];
        if(!viewport.intersects(box.adjusted(-2, -2, 2, 2)))
          continue;
        p.setBrush(palette().brush(QPalette::Base));
        p.setPen(QPen(palette().color(i == selectedNode ? QPalette::Highlight : QPalette::Mid),
                      i == selectedNode ? 3 : 1));
        p.drawRoundedRect(box, 6, 6);
        const QJsonObject node = nodes[i].toObject();
        const QSize pixels(int(std::ceil(box.width() * scale)), int(std::ceil(box.height() * scale)));
        // Keep the cached cards bounded at high zoom, where only part of a
        // card may be visible and direct clipped painting is cheaper.
        if(pixels.width() > 1024 || pixels.height() > 1024)
        {
          paintDiagramCardContent(p, node, i, box);
          continue;
        }
        QPixmap &pixmap = diagramCardContent[i];
        if(pixmap.isNull())
        {
          pixmap = QPixmap(pixels);
          pixmap.fill(Qt::transparent);
          QPainter cache(&pixmap);
          cache.setFont(font());
          cache.scale(scale, scale);
          cache.setRenderHints(QPainter::Antialiasing | QPainter::SmoothPixmapTransform);
          paintDiagramCardContent(cache, node, i, QRectF(QPointF(), box.size()));
        }
        const QRectF target(box.topLeft(), QSizeF(pixmap.size()) / scale);
        p.drawPixmap(target, pixmap, pixmap.rect());
      }
      return;
    }
    if(first.isNull() ||
       (card.value(QLatin1String("type")).toString() == QLatin1String("compare") &&
        second.isNull() && !identicalPair))
    {
      p.setPen(palette().color(QPalette::Text));
      p.drawText(
          rect(), Qt::AlignCenter,
          QCoreApplication::translate("AIVisualReply", "Image data is unavailable or changed."));
      return;
    }
    QJsonArray ink = card.value(QLatin1String("annotations")).toArray();
    QJsonArray ink2 = card.value(QLatin1String("secondAnnotations")).toArray();
    if(card.value(QLatin1String("type")).toString() == QLatin1String("compare"))
    {
      // A single supplied region mark describes both matched A/B views.
      // Explicit per-image marks still take precedence when their locations differ.
      if(ink.isEmpty())
        ink = ink2;
      if(ink2.isEmpty())
        ink2 = ink;
    }
    if(card.value(QLatin1String("type")).toString() == QLatin1String("mesh-uv"))
    {
      draw(p, first, ink, QRectF(0, 0, width() / 2.0, height()));
      draw(p, second, ink2, QRectF(width() / 2.0, 0, width() / 2.0, height()));
      imageText(p, QRectF(8, 8, width() / 2.0 - 16, 40), QLatin1String("3D"), Qt::white);
      imageText(p, QRectF(width() / 2.0 + 8, 8, width() / 2.0 - 16, 40), QLatin1String("UV"),
                Qt::white);
      return;
    }
    if(mode == DifferenceMode)
    {
      draw(p, differenceImage(), {}, rect());
      return;
    }
    if(second.isNull())
      draw(p, first, ink, rect());
    else if(mode == 0)
      draw(p, first, ink, rect());
    else if(mode == 1)
      draw(p, second, ink2, rect());
    else if(mode == 3)
    {
      draw(p, first, ink, QRectF(0, 0, width() / 2.0, height()));
      draw(p, second, ink2, QRectF(width() / 2.0, 0, width() / 2.0, height()));
    }
    else
    {
      draw(p, second, ink2, rect());
      p.save();
      p.setClipRect(QRectF(0, 0, width() * ratio, height()));
      draw(p, first, ink, rect());
      p.restore();
      p.setPen(QPen(Qt::white, 2));
      p.drawLine(QPointF(width() * ratio, 0), QPointF(width() * ratio, height()));
    }
    comparisonLabels(p);
  }
  void mousePressEvent(QMouseEvent *e) override
  {
    if(diagramEdits)
    {
      if(e->button() != Qt::LeftButton && e->button() != Qt::MiddleButton)
        return;
      diagramButton = e->button();
      QToolTip::hideText();
      diagramPress = last = e->localPos();
      diagramMoved = false;
      diagramGesture = DiagramGesture::Pan;
      if(diagramButton == Qt::LeftButton)
      {
        draggedNode = diagramNodeAt(diagramPress);
        const auto reroute = diagramRerouteAt(diagramPress);
        if(draggedNode >= 0)
        {
          diagramGesture = DiagramGesture::Node;
          diagramDragOrigin = diagramEdits->offsets[draggedNode];
        }
        else if(reroute.first >= 0)
        {
          diagramGesture = DiagramGesture::Reroute;
          draggedEdge = reroute.first;
          draggedReroute = reroute.second;
          diagramDragOrigin = diagramEdits->reroutes[draggedEdge][draggedReroute];
        }
      }
      return;
    }
    dragging = true;
    last = e->localPos();
    mouseMoveEvent(e);
  }
  void mouseMoveEvent(QMouseEvent *e) override
  {
    if(diagramEdits)
    {
      if(diagramButton == Qt::NoButton)
        return;
      if(!diagramMoved &&
         (e->localPos() - diagramPress).manhattanLength() < QApplication::startDragDistance())
        return;
      if(!diagramMoved)
      {
        diagramMoved = true;
        if(diagramGesture == DiagramGesture::Node)
          selectDiagramItem(draggedNode, -1);
        else if(diagramGesture == DiagramGesture::Reroute)
          selectDiagramItem(-1, draggedEdge);
      }
      if(diagramGesture == DiagramGesture::Pan)
      {
        pan += e->localPos() - last;
        last = e->localPos();
        update();
      }
      else
      {
        const QPointF position = diagramDragPosition(e->localPos());
        if(diagramGesture == DiagramGesture::Node)
        {
          QPointF &offset = diagramEdits->offsets[draggedNode];
          if(offset != position)
          {
            offset = position;
            diagramEdits->changed();
          }
        }
        else if(draggedReroute < diagramEdits->reroutes[draggedEdge].size() &&
                diagramCanPlaceReroute(position))
        {
          QPointF &reroute = diagramEdits->reroutes[draggedEdge][draggedReroute];
          if(reroute != position)
          {
            reroute = position;
            diagramEdits->changed();
          }
        }
      }
      return;
    }
    if(!dragging)
      return;
    if(!second.isNull() && mode == 2 && !(e->buttons() & Qt::MiddleButton))
    {
      ratio = qBound(0.0, e->localPos().x() / qMax(1, width()), 1.0);
      setProperty("visualWipeRatio", ratio);
      if(ratioChanged)
        ratioChanged(int(ratio * 100));
    }
    else
      pan += e->localPos() - last;
    last = e->localPos();
    update();
  }
  void mouseReleaseEvent(QMouseEvent *e) override
  {
    if(diagramEdits)
    {
      if(e->button() != diagramButton)
        return;
      finishDiagramDrag(e);
      if(diagramMoved || e->button() != Qt::LeftButton)
        return;
      const int node = diagramNodeAt(e->localPos());
      const int reroute = diagramRerouteAt(e->localPos()).first;
      selectDiagramItem(node,
                        node >= 0 ? -1 : (reroute >= 0 ? reroute : diagramEdgeAt(e->localPos())));
      if(node >= 0)
      {
        const int side = diagramChannelAt(node, e->localPos());
        if(side >= 0)
          showDiagramChannels(node, side, e->globalPos());
      }
      return;
    }
    dragging = false;
  }
  void mouseDoubleClickEvent(QMouseEvent *e) override
  {
    if(!diagramEdits || e->button() != Qt::LeftButton)
      return;
    const int i = diagramNodeAt(e->localPos());
    if(i < 0)
    {
      addDiagramReroute(e->localPos());
      return;
    }
    if(diagramChannelAt(i, e->localPos()) >= 0)
      return;
    const auto node = card.value(QLatin1String("nodes")).toArray()[i].toObject();
    const uint32_t eid = uint32_t(node.value(QLatin1String("eventId")).toDouble());
    if(eid && nodeClicked)
      nodeClicked(eid);
    else if(linkClicked && !node.value(QLatin1String("links")).toArray().isEmpty())
      linkClicked(visualResourceLink(QUrl(node.value(QLatin1String("links"))
                                              .toArray()[0]
                                              .toObject()
                                              .value(QLatin1String("url"))
                                              .toString()),
                                     uint32_t(card.value(QLatin1String("eventId")).toDouble())));
  }
  void wheelEvent(QWheelEvent *e) override
  {
    const QString kind = card.value(QLatin1String("type")).toString();
    const double nextZoom = qBound(kind == QLatin1String("diagram") ? .4 : 1.0,
                                   zoom * std::pow(1.2, e->angleDelta().y() / 120.0), 8.0);
    const QRectF bounds(rect());
    QPointF center = bounds.center();
#if QT_VERSION >= QT_VERSION_CHECK(5, 14, 0)
    const QPointF cursor = e->position();
#else
    const QPointF cursor = e->posF();
#endif
    QPointF anchor = center;
    if(bounds.contains(cursor))
    {
      anchor = cursor;
      if(kind == QLatin1String("mesh-uv") ||
         (kind == QLatin1String("compare") && mode == 3 && !second.isNull()))
        center.setX(width() * (cursor.x() < center.x() ? .25 : .75));
    }
    // Pan and the viewport center are the unscaled part of the drawing transform.
    pan += (anchor - center - pan) * (1.0 - nextZoom / zoom);
    zoom = nextZoom;
    update();
    e->accept();
  }
};

struct Face
{
  int a, b, c;
};
class MeshCanvas : public QWidget
{
public:
  QVector<QVector3D> vertices;
  QVector<QVector3D> normals;
  QVector<QPointF> uvs;
  QVector<Face> faces;
  QImage texture;
  QVector3D base = QVector3D(.65f, .68f, .75f);
  QVector3D environment = QVector3D(.2f, .2f, .2f);
  double environmentStrength = .25;
  double roughness = .4, metalness = 0, yaw = 0, pitch = 0, distance = 3.2;
  int shading = 0;
  QString effectType;
  double effectAmount = .04, frequency = 6, phase = 0;
  QColor effectColor = QColor(16, 16, 24);
  bool effectEnabled = true, compare = false, wavePlane = false;
  bool dragging = false;
  QPoint last;
  MeshCanvas(const QJsonObject &card, QWidget *parent) : QWidget(parent)
  {
    // i18n-ignore: stable native renderer identifier.
    setObjectName(QLatin1String("aiVisualMeshCanvas"));
    setMinimumSize(100, 100);
    const QJsonObject effect = card.value(QLatin1String("meshEffect")).toObject();
    effectType = effect.value(QLatin1String("type")).toString(QLatin1String("none"));
    effectAmount = qBound(0.0, effect.value(QLatin1String("amount")).toDouble(.04), .25);
    frequency = qBound(.1, effect.value(QLatin1String("frequency")).toDouble(6), 20.0);
    phase = qBound(-100.0, effect.value(QLatin1String("phase")).toDouble(), 100.0);
    effectEnabled = effect.value(QLatin1String("enabled")).toBool(true);
    const QColor ink(effect.value(QLatin1String("color")).toString());
    if(ink.isValid())
      effectColor = ink;
    const QJsonObject material = card.value(QLatin1String("material")).toObject();
    const QJsonArray rgb = material.value(QLatin1String("baseColor")).toArray();
    if(rgb.size() == 3)
      base = QVector3D(float(rgb[0].toDouble()), float(rgb[1].toDouble()), float(rgb[2].toDouble()));
    const QJsonArray env = material.value(QLatin1String("environmentColor")).toArray();
    if(env.size() == 3)
      environment =
          QVector3D(float(env[0].toDouble()), float(env[1].toDouble()), float(env[2].toDouble()));
    environmentStrength =
        qBound(0.0, material.value(QLatin1String("environmentStrength")).toDouble(.25), 2.0);
    roughness = qBound(.02, material.value(QLatin1String("roughness")).toDouble(.4), 1.0);
    metalness = qBound(0.0, material.value(QLatin1String("metalness")).toDouble(), 1.0);
    texture = imageAsset(material.value(QLatin1String("texture")).toObject());
    QJsonObject mesh = QJsonDocument::fromJson(blob(card.value(QLatin1String("geometry")).toObject(),
                                                    QLatin1String(".json")))
                           .object();
    const QJsonArray v = mesh.value(QLatin1String("vertices")).toArray(),
                     f = mesh.value(QLatin1String("triangles")).toArray();
    if(v.size() > 0 && v.size() <= 16384 && f.size() <= 16384)
    {
      for(const QJsonValue &item : v)
      {
        const QJsonArray p = item.toArray();
        if(p.size() != 3)
        {
          vertices.clear();
          break;
        }
        QVector3D point(float(p[0].toDouble()), float(p[1].toDouble()), float(p[2].toDouble()));
        if(!std::isfinite(point.x()) || !std::isfinite(point.y()) || !std::isfinite(point.z()))
        {
          vertices.clear();
          break;
        }
        vertices.push_back(point);
      }
      if(!vertices.isEmpty())
        for(const QJsonValue &item : f)
        {
          const QJsonArray p = item.toArray();
          if(p.size() != 3)
            continue;
          const int a = p[0].toInt(-1), b = p[1].toInt(-1), c = p[2].toInt(-1);
          if(a >= 0 && b >= 0 && c >= 0 && a < vertices.size() && b < vertices.size() &&
             c < vertices.size())
            faces.push_back({a, b, c});
        }
      const QJsonArray tex = mesh.value(QLatin1String("uvs")).toArray();
      if(tex.size() == vertices.size())
        for(const QJsonValue &item : tex)
        {
          const QJsonArray p = item.toArray();
          if(p.size() != 2 || !std::isfinite(p[0].toDouble()) || !std::isfinite(p[1].toDouble()) ||
             std::abs(p[0].toDouble()) > 1e6 || std::abs(p[1].toDouble()) > 1e6)
          {
            uvs.clear();
            break;
          }
          uvs.push_back(QPointF(p[0].toDouble(), p[1].toDouble()));
        }
    }
    const QString primitive = card.value(QLatin1String("primitive")).toString();
    if(vertices.isEmpty() && !primitive.isEmpty())
    {
      if(primitive == QLatin1String("sphere"))
      {
        for(int y = 0; y <= 16; y++)
          for(int x = 0; x <= 24; x++)
          {
            const double a = x * 2 * 3.14159265359 / 24, b = y * 3.14159265359 / 16;
            vertices.push_back(QVector3D(float(std::sin(b) * std::cos(a)), float(std::cos(b)),
                                         float(std::sin(b) * std::sin(a))));
            uvs.push_back(QPointF(x / 24.0, y / 16.0));
          }
        for(int y = 0; y < 16; y++)
          for(int x = 0; x < 24; x++)
          {
            const int a = y * 25 + x;
            faces.push_back({a, a + 1, a + 25});
            faces.push_back({a + 1, a + 26, a + 25});
          }
      }
      else if(primitive == QLatin1String("plane"))
      {
        wavePlane = effectType == QLatin1String("sine-displacement");
        const int steps = wavePlane ? 24 : 1;
        for(int y = 0; y <= steps; y++)
          for(int x = 0; x <= steps; x++)
          {
            const float horizontal = 2 * x / float(steps) - 1, vertical = 2 * y / float(steps) - 1;
            vertices.push_back(wavePlane ? QVector3D(horizontal, 0, -vertical)
                                         : QVector3D(horizontal, vertical, 0));
            uvs.push_back(QPointF(x / double(steps), y / double(steps)));
          }
        for(int y = 0; y < steps; y++)
          for(int x = 0; x < steps; x++)
          {
            const int a = y * (steps + 1) + x;
            faces.push_back({a, a + 1, a + steps + 2});
            faces.push_back({a, a + steps + 2, a + steps + 1});
          }
        if(wavePlane)
        {
          yaw = .25;
          pitch = .65;
        }
      }
      else
      {
        vertices = {{-1, -1, -1}, {1, -1, -1}, {1, 1, -1}, {-1, 1, -1},
                    {-1, -1, 1},  {1, -1, 1},  {1, 1, 1},  {-1, 1, 1}};
        faces = {{0, 1, 2}, {0, 2, 3}, {4, 6, 5}, {4, 7, 6}, {0, 4, 5}, {0, 5, 1},
                 {2, 6, 7}, {2, 7, 3}, {0, 3, 7}, {0, 7, 4}, {1, 5, 6}, {1, 6, 2}};
        for(Face &face : faces)
          std::swap(face.b, face.c);
      }
    }
    if(!vertices.isEmpty())
    {
      QVector3D lo = vertices[0], hi = lo;
      for(const QVector3D &p : vertices)
        for(int k = 0; k < 3; k++)
        {
          lo[k] = qMin(lo[k], p[k]);
          hi[k] = qMax(hi[k], p[k]);
        }
      const QVector3D center = (lo + hi) * .5f;
      const float span = qMax((hi - lo).length(), .000001f) * .5f;
      for(QVector3D &p : vertices)
        p = (p - center) / span;
    }
    normals.resize(vertices.size());
    for(const Face &face : faces)
    {
      const QVector3D normal = QVector3D::crossProduct(vertices[face.b] - vertices[face.a],
                                                       vertices[face.c] - vertices[face.a]);
      normals[face.a] += normal;
      normals[face.b] += normal;
      normals[face.c] += normal;
    }
    for(int i = 0; i < normals.size(); i++)
      normals[i] = (primitive == QLatin1String("sphere") ? vertices[i] : normals[i]).normalized();
    setProperty("visualMeshYaw", yaw);
    setProperty("visualMeshTriangleCount", faces.size());
  }
  void resetView()
  {
    yaw = wavePlane ? .25 : 0;
    pitch = wavePlane ? .65 : 0;
    distance = 3.2;
    setProperty("visualMeshYaw", yaw);
    update();
  }
  QImage renderImage(int w, int h, bool enabled) const
  {
    if(w <= 0 || h <= 0)
      return QImage();
    QImage img(w, h, QImage::Format_RGB32);
    img.fill(palette().color(QPalette::Dark));
    QVector<float> depth(w * h, std::numeric_limits<float>::max());
    const bool hullEnabled = enabled && effectType == QLatin1String("inverted-hull");
    QVector<QVector3D> transformed, projected;
    const double cy = std::cos(yaw), sy = std::sin(yaw), cp = std::cos(pitch), sp = std::sin(pitch);
    auto project = [&](bool hull) {
      transformed.clear();
      projected.clear();
      for(int index = 0; index < vertices.size(); index++)
      {
        QVector3D p = vertices[index];
        if(enabled && effectType == QLatin1String("sine-displacement"))
          p.setY(p.y() + float(std::sin(p.x() * frequency + phase) * effectAmount));
        if(hull)
          p += normals[index] * float(effectAmount);
        const double x = cy * p.x() + sy * p.z(), z = -sy * p.x() + cy * p.z(),
                     y = cp * p.y() - sp * z;
        const double zz = distance - (sp * p.y() + cp * z);
        transformed.push_back(QVector3D(float(x), float(y), float(zz)));
        projected.push_back(QVector3D(float(w * .5 + x * h * .95 / zz),
                                      float(h * .5 - y * h * .95 / zz), float(zz)));
      }
    };
    const QVector3D light = QVector3D(-.4f, .7f, -1).normalized(),
                    halfLight = (light + QVector3D(0, 0, -1)).normalized();
    for(int pass = 0; pass < (hullEnabled ? 2 : 1); pass++)
    {
      const bool hull = pass == 1;
      project(hull);
      for(const Face &face : faces)
      {
        const QVector3D a = projected[face.a], b = projected[face.b], c = projected[face.c];
        const float denominator =
            (b.y() - c.y()) * (a.x() - c.x()) + (c.x() - b.x()) * (a.y() - c.y());
        if(std::abs(denominator) < .0001f || (hull && denominator < 0))
          continue;
        QVector3D normal = QVector3D::crossProduct(transformed[face.b] - transformed[face.a],
                                                   transformed[face.c] - transformed[face.a])
                               .normalized();
        if(normal.z() > 0)
          normal = -normal;
        if(shading == 2)
        {
          QPainter line(&img);
          line.setPen(hull ? effectColor : QColor(130, 215, 240));
          line.drawPolygon(QPolygonF({a.toPointF(), b.toPointF(), c.toPointF()}));
          continue;
        }
        const float diffuse = qMax(0.0f, QVector3D::dotProduct(normal, light));
        const float spec = float(std::pow(qMax(0.0f, QVector3D::dotProduct(normal, halfLight)),
                                          2 + 120 * (1 - roughness) * (1 - roughness)) *
                                 (.3 + .7 * metalness));
        const int x0 = qBound(0, int(std::floor(qMin(a.x(), qMin(b.x(), c.x())))), w - 1),
                  x1 = qBound(0, int(std::ceil(qMax(a.x(), qMax(b.x(), c.x())))), w - 1);
        const int y0 = qBound(0, int(std::floor(qMin(a.y(), qMin(b.y(), c.y())))), h - 1),
                  y1 = qBound(0, int(std::ceil(qMax(a.y(), qMax(b.y(), c.y())))), h - 1);
        for(int y = y0; y <= y1; y++)
        {
          QRgb *row = reinterpret_cast<QRgb *>(img.scanLine(y));
          for(int x = x0; x <= x1; x++)
          {
            const float u =
                ((b.y() - c.y()) * (x + .5f - c.x()) + (c.x() - b.x()) * (y + .5f - c.y())) /
                denominator;
            const float v = ((c.y() - a.y()) * (x + .5f - c.x()) +
                             (a.x() - c.x()) * (y + .5f - c.y())) /
                            denominator,
                        t = 1 - u - v;
            if(u < 0 || v < 0 || t < 0)
              continue;
            const float iz = u / a.z() + v / b.z() + t / c.z(), z = 1 / iz;
            if(z >= depth[y * w + x])
              continue;
            depth[y * w + x] = z;
            QVector3D color = base;
            if(!texture.isNull() && uvs.size() == vertices.size())
            {
              QPointF uv = (uvs[face.a] * (u / a.z()) + uvs[face.b] * (v / b.z()) +
                            uvs[face.c] * (t / c.z())) /
                           iz;
              const double tx = uv.x() - std::floor(uv.x()), ty = uv.y() - std::floor(uv.y());
              QRgb tex =
                  texture.pixel(qBound(0, int(tx * texture.width()), texture.width() - 1),
                                qBound(0, int((1 - ty) * texture.height()), texture.height() - 1));
              color = QVector3D(qRed(tex) / 255.f, qGreen(tex) / 255.f, qBlue(tex) / 255.f) * color;
            }
            color = shading == 1
                        ? (normal + QVector3D(1, 1, 1)) * .5f
                        : color * float(.18 + diffuse * (1 - .65 * metalness)) +
                              QVector3D(spec, spec, spec) +
                              environment * float(environmentStrength * (.15 + .85 * metalness) *
                                                  (1 - .6 * roughness));
            if(hull)
              color = QVector3D(float(effectColor.redF()), float(effectColor.greenF()),
                                float(effectColor.blueF()));
            row[x] = qRgb(qBound(0, int(color.x() * 255), 255), qBound(0, int(color.y() * 255), 255),
                          qBound(0, int(color.z() * 255), 255));
          }
        }
      }
    }
    return img;
  }
protected:
  void paintEvent(QPaintEvent *) override
  {
    const int w = qMin(width(), 900), h = qMin(height(), 600);
    if(w <= 0 || h <= 0)
      return;
    QPainter painter(this);
    if(vertices.isEmpty() || faces.isEmpty())
    {
      painter.fillRect(rect(), palette().color(QPalette::Dark));
      painter.setPen(palette().color(QPalette::Text));
      painter.drawText(
          rect(), Qt::AlignCenter,
          QCoreApplication::translate("AIVisualReply", "Geometry data is unavailable or changed."));
      return;
    }
    if(!compare)
      painter.drawImage(rect(), renderImage(w, h, effectEnabled));
    else
    {
      painter.drawImage(QRect(0, 0, width() / 2, height()), renderImage(w / 2, h, false));
      painter.drawImage(QRect(width() / 2, 0, width() - width() / 2, height()),
                        renderImage(w - w / 2, h, effectEnabled));
      painter.setPen(palette().color(QPalette::Text));
      painter.drawLine(width() / 2, 0, width() / 2, height());
      painter.drawText(QRect(6, 4, width() / 2 - 12, 24), Qt::AlignLeft,
                       QCoreApplication::translate("AIVisualReply", "Before"));
      painter.drawText(QRect(width() / 2 + 6, 4, width() / 2 - 12, 24), Qt::AlignLeft,
                       QCoreApplication::translate("AIVisualReply", "After"));
    }
  }
  void mousePressEvent(QMouseEvent *e) override
  {
    dragging = true;
    last = e->pos();
  }
  void mouseMoveEvent(QMouseEvent *e) override
  {
    if(!dragging)
      return;
    const QPoint delta = e->pos() - last;
    last = e->pos();
    yaw += delta.x() * .012;
    pitch = qBound(-1.5, pitch + delta.y() * .012, 1.5);
    setProperty("visualMeshYaw", yaw);
    update();
  }
  void mouseReleaseEvent(QMouseEvent *) override { dragging = false; }
  void wheelEvent(QWheelEvent *e) override
  {
    distance = qBound(1.6, distance * std::pow(.85, e->angleDelta().y() / 120.0), 12.0);
    update();
    e->accept();
  }
};
}

bool AIVisualReply::validIdentity(const QString &id)
{
  static const QRegularExpression expression(QLatin1String("^[0-9a-f]{64}$"));
  return expression.match(id).hasMatch();
}

QString AIVisualReply::storageRoot()
{
  QString root = QString::fromLocal8Bit(qgetenv("LOCALAPPDATA"));
  if(root.isEmpty())
    root = QString::fromLocal8Bit(qgetenv("XDG_DATA_HOME"));
  if(root.isEmpty())
    root = QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation);
  return QDir::cleanPath(root + QLatin1String("/ReplayLab/ai-visuals"));
}

QImage AIVisualReply::verifiedImage(const QJsonObject &asset)
{
  return imageAsset(asset, true);
}

QStringList AIVisualReply::visibleIdentities(const QJsonArray &references)
{
  QStringList identities;
  QSet<QByteArray> seen;
  for(int i = references.size() - 1; i >= qMax(0, references.size() - 8); --i)
  {
    const QString identity = references[i].toObject().value(QLatin1String("id")).toString();
    if(!validIdentity(identity))
      continue;
    const QByteArray raw = readVerified(
        storageRoot() + QLatin1Char('/') + identity + QLatin1String(".json"), identity, 256 * 1024);
    const QJsonObject manifest = QJsonDocument::fromJson(raw).object();
    if(manifest.value(QLatin1String("schema")).toString() != QLatin1String("bread-ai-visual/v1"))
      continue;
    QByteArray key = identity.toLatin1();
    if(manifest.value(QLatin1String("schema")).toString() == QLatin1String("bread-ai-visual/v1"))
      key = QCryptographicHash::hash(
          QJsonDocument(visibleContent(manifest).toObject()).toJson(QJsonDocument::Compact),
          QCryptographicHash::Sha256);
    if(seen.contains(key))
      continue;
    seen.insert(key);
    identities.prepend(identity);
  }
  // A publication revises this answer's one viewer. Historical immutable assets
  // remain available; the final bundle contains all of its focused cards.
  return identities.isEmpty() ? identities : QStringList{identities.last()};
}

QJsonObject AIVisualReply::navigationTarget(const QString &identity)
{
  if(!validIdentity(identity))
    return {};
  const QJsonObject result =
      QJsonDocument::fromJson(readVerified(storageRoot() + QLatin1String("/targets/") + identity +
                                               QLatin1String(".json"),
                                           identity, 8 * 1024 * 1024))
          .object();
  return result.value(QLatin1String("schema")).toString() == QLatin1String("bread-ai-navigation/v1")
             ? result
             : QJsonObject();
}

QUrl AIVisualReply::resolveLegacyTextureLink(const QUrl &url, const QString &identity,
                                             QJsonObject &capture)
{
  if(!validIdentity(identity))
    return {};
  const QJsonObject manifest =
      QJsonDocument::fromJson(
          readVerified(storageRoot() + QLatin1Char('/') + identity + QLatin1String(".json"),
                       identity, 256 * 1024))
          .object();
  QSet<uint32_t> events;
  auto collect = [&](const QJsonObject &item, uint32_t fallbackEid) {
    const uint32_t ownEid = uint32_t(item.value(QLatin1String("eventId")).toDouble());
    const uint32_t eid = ownEid ? ownEid : fallbackEid;
    auto add = [&](const QUrl &target) {
      if(target.scheme() != QLatin1String("rdoc") || target.path() != url.path() ||
         (target.host() != QLatin1String("texture") && target.host() != QLatin1String("resource")))
        return;
      const uint32_t targetEid =
          QUrlQuery(visualResourceLink(target, eid)).queryItemValue(QLatin1String("eid")).toUInt();
      if(targetEid)
        events.insert(targetEid);
    };
    for(const QJsonValue &link : item.value(QLatin1String("links")).toArray())
      add(QUrl(link.toObject().value(QLatin1String("url")).toString()));
    auto matches =
        explanationLinkPattern().globalMatch(item.value(QLatin1String("details")).toString());
    while(matches.hasNext())
      add(QUrl(matches.next().captured(2)));
  };
  for(const QJsonValue &value : manifest.value(QLatin1String("cards")).toArray())
  {
    const QJsonObject card = value.toObject();
    collect(card, 0);
    const uint32_t eid = uint32_t(card.value(QLatin1String("eventId")).toDouble());
    for(const QJsonValue &node : card.value(QLatin1String("nodes")).toArray())
      collect(node.toObject(), eid);
  }
  if(events.size() != 1)
    return {};
  capture = manifest.value(QLatin1String("capture")).toObject();
  return visualResourceLink(url, *events.constBegin());
}

int AIVisualReply::inlineHeight(const QString &identity)
{
  if(!validIdentity(identity))
    return 300;
  const QJsonObject manifest =
      QJsonDocument::fromJson(
          readVerified(storageRoot() + QLatin1Char('/') + identity + QLatin1String(".json"),
                       identity, 256 * 1024))
          .object();
  int height = 300;
  bool stageCode = false;
  for(const QJsonValue &value : manifest.value(QLatin1String("cards")).toArray())
  {
    const QString kind = value.toObject().value(QLatin1String("type")).toString();
    height = qMax(height, kind == QLatin1String("mesh")   ? 500
                          : kind == QLatin1String("code") ? 320
                                                          : 400);
    for(const auto &node : value.toObject().value(QLatin1String("nodes")).toArray())
      stageCode |= !node.toObject().value(QLatin1String("codeBlocks")).toArray().isEmpty();
  }
  return !stageCode && manifest.value(QLatin1String("cards")).toArray().size() == 1 &&
                 manifest.value(QLatin1String("cards"))
                         .toArray()[0]
                         .toObject()
                         .value(QLatin1String("type"))
                         .toString() == QLatin1String("diagram")
             ? 320
             : height;
}

AIVisualReply::AIVisualReply(const QString &identity, Navigate navigate, QWidget *parent)
    : AIVisualReply(identity, std::move(navigate), parent,
                    std::make_shared<AIVisualReplyLayoutState>())
{
}

AIVisualReply::AIVisualReply(const QString &identity, Navigate navigate, QWidget *parent,
                             std::shared_ptr<AIVisualReplyLayoutState> layouts)
    : QWidget(parent)
{
  // i18n-ignore: stable object identifier for native UI and regression checks.
  setObjectName(QLatin1String("aiVisualReply"));
  setProperty("visualIdentity", identity);
  QVBoxLayout *layout = new QVBoxLayout(this);
  layout->setContentsMargins(5, 3, 5, 3);
  layout->setSpacing(3);
  const QByteArray manifestBytes =
      validIdentity(identity)
          ? readVerified(storageRoot() + QLatin1Char('/') + identity + QLatin1String(".json"),
                         identity, 256 * 1024)
          : QByteArray();
  const QJsonObject manifest = QJsonDocument::fromJson(manifestBytes).object();
  QJsonArray cards = manifest.value(QLatin1String("cards")).toArray();
  if(manifest.value(QLatin1String("schema")).toString() != QLatin1String("bread-ai-visual/v1") ||
     cards.isEmpty() || cards.size() > 6)
  {
    layout->addWidget(new QLabel(
        tr("Visual explanation unavailable. Its saved data is missing or changed."), this));
    return;
  }
  // Group comparisons first, preserving the order within each group and the saved bundle.
  QJsonArray orderedCards;
  for(bool compare : {true, false})
    for(const QJsonValue &value : cards)
      if((value.toObject().value(QLatin1String("type")).toString() == QLatin1String("compare")) ==
         compare)
        orderedCards.append(value);
  cards = orderedCards;

  // Stage source follows the visual cards. The detail pane links to the corresponding code page.
  QJsonArray stagePages;
  const int visualCount = cards.size();
  for(int i = 0; i < visualCount; ++i)
  {
    auto card = cards[i].toObject();
    if(card.value(QLatin1String("type")).toString() != QLatin1String("diagram"))
      continue;
    auto nodes = card.value(QLatin1String("nodes")).toArray();
    for(int j = 0; j < nodes.size(); ++j)
    {
      auto node = nodes[j].toObject();
      const auto blocks = node.value(QLatin1String("codeBlocks")).toArray();
      if(blocks.isEmpty())
        continue;
      node[QLatin1String("codePage")] = visualCount + stagePages.size();
      nodes[j] = node;
      stagePages.append(QJsonObject{
          {QLatin1String("type"), QLatin1String("code")},
          {QLatin1String("title"), tr("Code: %1").arg(node.value(QLatin1String("text")).toString())},
          {QLatin1String("codeBlocks"), blocks},
          {QLatin1String("eventId"), node.value(QLatin1String("eventId"))},
          {QLatin1String("links"), node.value(QLatin1String("links"))}});
    }
    card[QLatin1String("nodes")] = nodes;
    cards[i] = card;
  }
  for(const auto &page : stagePages)
    cards.append(page);
  const QJsonObject capture = manifest.value(QLatin1String("capture")).toObject();
  QHBoxLayout *header = new QHBoxLayout();
  QComboBox *selector = new QComboBox(this);
  selector->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);
  selector->setMinimumWidth(80);
  for(const QJsonValue &value : cards)
    selector->addItem(
        presentationTitle(value.toObject().value(QLatin1String("title")).toString()).left(160));
  header->addWidget(selector, 1);
  QSlider *pages = new QSlider(Qt::Horizontal, this);
  pages->setObjectName(QLatin1String("aiVisualPageSlider"));    // i18n-ignore: automation identifier.
  pages->setRange(0, cards.size() - 1);
  pages->setSingleStep(1);
  pages->setPageStep(1);
  pages->setTickPosition(QSlider::TicksBelow);
  pages->setTickInterval(1);
  pages->setMinimumWidth(44);
  pages->setMaximumWidth(100);
  pages->setEnabled(cards.size() > 1);
  pages->setAccessibleName(tr("Explanation page"));
  pages->setToolTip(tr("Drag to switch explanations"));
  header->addWidget(pages);
  connect(pages, &QSlider::valueChanged, selector, &QComboBox::setCurrentIndex);
  connect(selector, static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged), pages,
          &QSlider::setValue);
  QPushButton *expand = new QPushButton(tr("Expand"), this);
  header->addWidget(expand);
  layout->addLayout(header);
  connect(expand, &QPushButton::clicked, this, [this, identity, navigate, selector, layouts]() {
    QDialog *dialog = new QDialog(window());
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    dialog->setWindowTitle(tr("Visual explanation"));
    dialog->resize(1000, 760);
    QVBoxLayout *body = new QVBoxLayout(dialog);
    AIVisualReply *reply = new AIVisualReply(identity, navigate, dialog, layouts);
    reply->setComparisonMode(m_ComparisonMode);
    if(QComboBox *combo = reply->findChild<QComboBox *>())
      combo->setCurrentIndex(selector->currentIndex());
    body->addWidget(reply);
    dialog->show();
  });
  QStackedWidget *stack = new QStackedWidget(this);
  layout->addWidget(stack, 1);
  for(const QJsonValue &value : cards)
  {
    const QJsonObject card = value.toObject();
    QWidget *page = new QWidget(stack);
    QVBoxLayout *body = new QVBoxLayout(page);
    body->setContentsMargins(0, 0, 0, 0);
    body->setSpacing(3);
    const QString kind = card.value(QLatin1String("type")).toString();
    if(kind != QLatin1String("image") && kind != QLatin1String("compare") &&
       kind != QLatin1String("mesh") && kind != QLatin1String("diagram") &&
       kind != QLatin1String("code") && kind != QLatin1String("mesh-uv"))
    {
      body->addWidget(new QLabel(tr("This visual card is invalid."), page));
      stack->addWidget(page);
      continue;
    }
    QLabel *caption = new QLabel(card.value(QLatin1String("caption")).toString().left(1600), page);
    caption->setTextFormat(Qt::PlainText);
    caption->setWordWrap(true);
    caption->setMaximumHeight(44);
    caption->setToolTip(caption->text());
    if(!caption->text().isEmpty() && kind != QLatin1String("compare") && kind != QLatin1String("code"))
      body->addWidget(caption);
    else
      caption->hide();
    if(kind == QLatin1String("code"))
    {
      QLabel *hint = new QLabel(tr("Click code to open the Shader calculation."), page);
      hint->setWordWrap(true);
      body->addWidget(hint);
      QTextBrowser *source = new QTextBrowser(page);
      source->setObjectName(QLatin1String("aiVisualCode"));
      source->setOpenLinks(false);
      source->setOpenExternalLinks(false);
      auto blocks = card.value(QLatin1String("codeBlocks")).toArray();
      if(blocks.isEmpty())
        blocks.append(card.value(QLatin1String("code")));
      QString html;
      for(const auto &blockValue : blocks)
      {
        const auto code = blockValue.toObject();
        const QString title = code.value(QLatin1String("title")).toString();
        if(!title.isEmpty())
          html += QLatin1String("<h4>") + title.toHtmlEscaped() + QLatin1String("</h4>");
        html += codeHtml(code, source->palette());
      }
      source->setHtml(html);
      body->addWidget(source, 1);
      connect(source, &QTextBrowser::anchorClicked, page, [navigate, capture](const QUrl &url) {
        if(navigate)
          navigate(url, capture);
      });
    }
    else if(kind == QLatin1String("mesh"))
    {
      const bool effectDemo = card.value(QLatin1String("meshEffect"))
                                  .toObject()
                                  .value(QLatin1String("type"))
                                  .toString(QLatin1String("none")) != QLatin1String("none");
      QLabel *badge =
          new QLabel(effectDemo ? tr("Algorithm illustration") : tr("Material preview"), page);
      badge->setWordWrap(true);
      body->addWidget(badge);
      const QJsonObject info = card.value(QLatin1String("geometryInfo")).toObject();
      if(!info.isEmpty())
      {
        QLabel *scope = new QLabel(
            info.value(QLatin1String("space")).toString() == QLatin1String("approximate-view")
                ? tr("Captured geometry; approximate projection")
                : tr("Exported geometry"),
            page);
        if(!info.value(QLatin1String("complete")).toBool())
          scope->setText(tr("Partial captured geometry"));
        body->addWidget(scope);
      }
      MeshCanvas *mesh = new MeshCanvas(card, page);
      body->addWidget(mesh, 1);
      QWidget *materialPanel = new QWidget(page);
      QVBoxLayout *materialLayout = new QVBoxLayout(materialPanel);
      materialLayout->setContentsMargins(0, 0, 0, 0);
      materialLayout->setSpacing(3);
      body->addWidget(materialPanel);
      materialPanel->setVisible(!effectDemo);
      QHBoxLayout *controls = new QHBoxLayout();
      QComboBox *mode = new QComboBox(page);
      mode->addItems({tr("Material"), tr("Normals"), tr("Wireframe")});
      controls->addWidget(mode);
      connect(mode, static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged), mesh,
              [mesh](int n) {
                mesh->shading = n;
                mesh->update();
              });
      QLabel *name = new QLabel(tr("Roughness"), page);
      controls->addWidget(name);
      QSlider *roughness = new QSlider(Qt::Horizontal, page);
      roughness->setRange(2, 100);
      roughness->setValue(int(mesh->roughness * 100));
      controls->addWidget(roughness, 1);
      // i18n-ignore: stable object identifier for native UI checks.
      roughness->setObjectName(QLatin1String("aiVisualRoughness"));
      connect(roughness, &QSlider::valueChanged, mesh, [mesh](int n) {
        mesh->roughness = n / 100.0;
        mesh->setProperty("visualRoughness", mesh->roughness);
        mesh->update();
      });
      materialLayout->addLayout(controls);
      QHBoxLayout *materialControls = new QHBoxLayout();
      materialControls->addWidget(new QLabel(tr("Metalness"), page));
      QSlider *metalness = new QSlider(Qt::Horizontal, page);
      metalness->setRange(0, 100);
      metalness->setValue(int(mesh->metalness * 100));
      materialControls->addWidget(metalness, 1);
      connect(metalness, &QSlider::valueChanged, mesh, [mesh](int n) {
        mesh->metalness = n / 100.0;
        mesh->update();
      });
      QPushButton *color = new QPushButton(tr("Base color"), page);
      materialControls->addWidget(color);
      connect(color, &QPushButton::clicked, mesh, [mesh, page]() {
        const QColor selected = QColorDialog::getColor(
            QColor::fromRgbF(mesh->base.x(), mesh->base.y(), mesh->base.z()), page,
            QCoreApplication::translate("AIVisualReply", "Illustrative base color"));
        if(selected.isValid())
        {
          mesh->base =
              QVector3D(float(selected.redF()), float(selected.greenF()), float(selected.blueF()));
          mesh->update();
        }
      });
      QPushButton *reset = new QPushButton(tr("Reset view"), page);
      materialControls->addWidget(reset);
      connect(reset, &QPushButton::clicked, mesh, [mesh]() { mesh->resetView(); });
      materialLayout->addLayout(materialControls);
      QHBoxLayout *environmentControls = new QHBoxLayout();
      environmentControls->addWidget(new QLabel(tr("Environment strength"), page));
      QSlider *envStrength = new QSlider(Qt::Horizontal, page);
      envStrength->setRange(0, 200);
      envStrength->setValue(int(mesh->environmentStrength * 100));
      environmentControls->addWidget(envStrength, 1);
      // i18n-ignore: stable object identifier for native UI checks.
      envStrength->setObjectName(QLatin1String("aiVisualEnvironmentStrength"));
      connect(envStrength, &QSlider::valueChanged, mesh, [mesh](int n) {
        mesh->environmentStrength = n / 100.0;
        mesh->update();
      });
      materialLayout->addLayout(environmentControls);
      if(effectDemo)
      {
        QHBoxLayout *effectControls = new QHBoxLayout();
        QCheckBox *enabled = new QCheckBox(mesh->effectType == QLatin1String("inverted-hull")
                                               ? tr("Inverted hull")
                                               : tr("Vertex displacement"),
                                           page);
        // i18n-ignore: stable control identifiers for bounded preview parameters.
        enabled->setObjectName(QLatin1String("aiVisualEffectEnabled"));
        enabled->setChecked(mesh->effectEnabled);
        effectControls->addWidget(enabled);
        connect(enabled, &QCheckBox::toggled, mesh, [mesh](bool on) {
          mesh->effectEnabled = on;
          mesh->setProperty("visualMeshEffectEnabled", on);
          mesh->update();
        });
        effectControls->addWidget(new QLabel(mesh->effectType == QLatin1String("inverted-hull")
                                                 ? tr("Extrusion width")
                                                 : tr("Displacement amount"),
                                             page));
        QDoubleSpinBox *amount = new QDoubleSpinBox(page);
        amount->setRange(0, .25);
        amount->setDecimals(3);
        amount->setSingleStep(.005);
        amount->setValue(mesh->effectAmount);
        amount->setObjectName(QLatin1String("aiVisualEffectAmount"));
        amount->setToolTip(tr("Preview units; this control does not edit the capture."));
        effectControls->addWidget(amount);
        connect(amount, QOverload<double>::of(&QDoubleSpinBox::valueChanged), mesh, [mesh](double n) {
          mesh->effectAmount = n;
          mesh->setProperty("visualMeshEffectAmount", n);
          mesh->update();
        });
        QCheckBox *comparison = new QCheckBox(tr("Side by side"), page);
        comparison->setObjectName(QLatin1String("aiVisualMeshCompare"));
        effectControls->addWidget(comparison);
        connect(comparison, &QCheckBox::toggled, mesh, [mesh](bool on) {
          mesh->compare = on;
          mesh->setProperty("visualMeshCompare", on);
          mesh->update();
        });
        QToolButton *details = new QToolButton(page);
        details->setText(tr("Preview settings"));
        details->setCheckable(true);
        effectControls->addWidget(details);
        connect(details, &QToolButton::toggled, materialPanel, &QWidget::setVisible);
        body->addLayout(effectControls);
        if(mesh->effectType == QLatin1String("inverted-hull"))
        {
          QPushButton *outlineColor = new QPushButton(tr("Outline color"), page);
          connect(outlineColor, &QPushButton::clicked, mesh, [mesh, page]() {
            const QColor selected = QColorDialog::getColor(
                mesh->effectColor, page,
                QCoreApplication::translate("AIVisualReply", "Outline color"));
            if(selected.isValid())
            {
              mesh->effectColor = selected;
              mesh->update();
            }
          });
          materialLayout->addWidget(outlineColor);
        }
        if(mesh->effectType == QLatin1String("sine-displacement"))
        {
          QHBoxLayout *wave = new QHBoxLayout();
          wave->addWidget(new QLabel(tr("Frequency"), page));
          QDoubleSpinBox *frequency = new QDoubleSpinBox(page);
          frequency->setRange(.1, 20);
          frequency->setSingleStep(.1);
          frequency->setValue(mesh->frequency);
          wave->addWidget(frequency);
          connect(frequency, QOverload<double>::of(&QDoubleSpinBox::valueChanged), mesh,
                  [mesh](double n) {
                    mesh->frequency = n;
                    mesh->update();
                  });
          wave->addWidget(new QLabel(tr("Phase"), page));
          QDoubleSpinBox *phase = new QDoubleSpinBox(page);
          phase->setRange(-100, 100);
          phase->setSingleStep(.1);
          phase->setValue(mesh->phase);
          phase->setObjectName(QLatin1String("aiVisualEffectPhase"));
          wave->addWidget(phase);
          connect(phase, QOverload<double>::of(&QDoubleSpinBox::valueChanged), mesh, [mesh](double n) {
            mesh->phase = n;
            mesh->update();
          });
          materialLayout->addLayout(wave);
        }
        QLabel *scope = new QLabel(tr("Preview normals and units are illustrative; use the linked "
                                      "Shader/CBuffer for capture controls."),
                                   page);
        scope->setWordWrap(true);
        materialLayout->addWidget(scope);
        // i18n-ignore: these are the exact bounded preview calculations, not UI prose or captured source.
        const QString formula =
            mesh->effectType == QLatin1String("inverted-hull")
                ? QLatin1String("position += normalize(normal) * width; // Cull Front")
                : QLatin1String("position.y += sin(position.x * frequency + phase) * amount;");
        bool comment = false;
        QLabel *calculation = new QLabel(page);
        calculation->setTextFormat(Qt::RichText);
        calculation->setWordWrap(true);
        calculation->setText(QLatin1String("<code>") +
                             AICodeFormatting::highlightedSourceLine(formula, QLatin1String("hlsl"),
                                                                     comment, page->palette()) +
                             QLatin1String("</code>"));
        materialLayout->addWidget(calculation);
      }
      mesh->setToolTip(
          tr("Drag to rotate; wheel to zoom. Material controls affect this illustration only."));
    }
    else
    {
      std::shared_ptr<AIVisualReplyLayoutState::Diagram> edits;
      if(kind == QLatin1String("diagram"))
      {
        auto &entry = layouts->diagrams[stack->count()];
        if(!entry)
          entry = std::make_shared<AIVisualReplyLayoutState::Diagram>();
        edits = entry;
      }
      VisualCanvas *view = new VisualCanvas(card, page, edits);
      QSplitter *flowPanels = NULL;
      if(kind == QLatin1String("diagram"))
      {
        flowPanels = new QSplitter(Qt::Vertical, page);
        flowPanels->setObjectName(QLatin1String("aiVisualFlowPanels"));
        flowPanels->setChildrenCollapsible(false);
        flowPanels->addWidget(view);
        body->addWidget(flowPanels, 1);
      }
      else
        body->addWidget(view, 1);
      view->nodeClicked = [navigate, capture](uint32_t eid) {
        if(eid && navigate)
          navigate(QUrl(QLatin1String("rdoc://event/") + QString::number(eid)), capture);
      };
      view->linkClicked = [navigate, capture](const QUrl &url) {
        if(navigate)
          navigate(url, capture);
      };
      view->setToolTip(tr("Wheel to zoom; drag to move the image or comparison divider."));
      QToolButton *flip = nullptr;
      if(kind == QLatin1String("image") || kind == QLatin1String("compare") ||
         kind == QLatin1String("mesh-uv"))
      {
        flip = new QToolButton(page);
        flip->setText(tr("Flip Y"));
        flip->setCheckable(true);
        if(kind != QLatin1String("compare"))
          body->addWidget(flip, 0, Qt::AlignRight);
        connect(flip, &QToolButton::toggled, view, [view](bool on) {
          view->flipY = on;
          view->pan = {};
          view->update();
        });
      }
      if(kind == QLatin1String("compare"))
      {
        QHBoxLayout *controls = new QHBoxLayout();
        QButtonGroup *modes = new QButtonGroup(page);
        modes->setExclusive(true);
        const QString names[] = {tr("Before / After"), tr("Wipe"), tr("Side by side"),
                                 tr("Difference")};
        for(int i : {0, 2, 3, VisualCanvas::DifferenceMode})
        {
          QToolButton *button = new QToolButton(page);
          button->setText(names[i == 0 ? 0 : i - 1]);
          button->setCheckable(true);
          modes->addButton(button, i);
          controls->addWidget(button);
          if(i == 0)
          {
            button->setObjectName(QLatin1String("aiVisualToggleAB"));
            button->setToolTip(tr("Click to switch between the two images"));
          }
          if(i == VisualCanvas::DifferenceMode)
          {
            button->setObjectName(
                QLatin1String("aiVisualDifference"));    // i18n-ignore: test identifier.
            button->setToolTip(tr(
                "Compare matching pixels. Black means no change; brighter pixels changed more."));
          }
          if(view->second.isNull() ||
             ((i == 2 || i == VisualCanvas::DifferenceMode) && !view->hasAlignedPair()))
            button->setEnabled(false);
          connect(button, &QToolButton::clicked, view,
                  [this, view, i]() { setComparisonMode(i == 0 ? (view->mode == 0 ? 1 : 0) : i); });
        }
        QSlider *divider = new QSlider(Qt::Horizontal, page);
        divider->setRange(0, 100);
        divider->setValue(50);
        divider->setEnabled(view->hasAlignedPair());
        controls->addWidget(divider, 1);
        // i18n-ignore: stable object identifier for native UI checks.
        divider->setObjectName(QLatin1String("aiVisualDivider"));
        view->ratioChanged = [divider](int n) { divider->setValue(n); };
        view->modeChanged = [modes](int i) { modes->button(i < 2 ? 0 : i)->setChecked(true); };
        connect(divider, &QSlider::valueChanged, view, [this, view](int n) {
          view->ratio = n / 100.0;
          view->setProperty("visualWipeRatio", view->ratio);
          setComparisonMode(2);
        });
        view->applyComparisonMode(m_ComparisonMode);
        const QJsonObject experiment = card.value(QLatin1String("experiment")).toObject();
        const QJsonArray roi = experiment.value(QLatin1String("roi")).toArray();
        if(experiment.value(QLatin1String("verified")).toBool() && roi.size() == 4)
        {
          const double left = qBound(0.0, roi[0].toDouble(), 1.0),
                       top = qBound(0.0, roi[1].toDouble(), 1.0);
          const double right = qBound(left, roi[2].toDouble(), 1.0),
                       bottom = qBound(top, roi[3].toDouble(), 1.0);
          const double dx = qMax(.01, (right - left) * .5), dy = qMax(.01, (bottom - top) * .5);
          const QRectF region(QPointF(qMax(0.0, left - dx), qMax(0.0, top - dy)),
                              QPointF(qMin(1.0, right + dx), qMin(1.0, bottom + dy)));
          if(right > left && bottom > top)
          {
            QToolButton *focus = new QToolButton(page);
            focus->setText(tr("Focus region"));
            focus->setCheckable(true);
            // i18n-ignore: stable object identifier for comparison focus checks.
            focus->setObjectName(QLatin1String("aiVisualFocusRegion"));
            controls->addWidget(focus);
            connect(focus, &QToolButton::toggled, view, [view, region](bool checked) {
              view->focus = checked ? region : QRectF();
              view->zoom = 1;
              view->pan = {};
              view->setProperty("visualRegionFocused", checked);
              view->update();
            });
            focus->setChecked(true);
          }
        }
        controls->addWidget(flip);
        body->addLayout(controls);
      }
      else if(kind == QLatin1String("diagram"))
      {
        QWidget *detailPanel = new QWidget(flowPanels);
        QVBoxLayout *detailLayout = new QVBoxLayout(detailPanel);
        detailLayout->setContentsMargins(0, 0, 0, 0);
        QHBoxLayout *flowControls = new QHBoxLayout();
        QLabel *flowHelp = new QLabel(
            tr("Drag nodes to arrange them. Select a node or wire for details. Double-click a "
               "node to navigate or a wire to add a reroute point."),
            detailPanel);
        flowHelp->setWordWrap(true);
        flowControls->addWidget(flowHelp, 1);
        QPushButton *resetLayout = new QPushButton(tr("Reset layout"), detailPanel);
        connect(resetLayout, &QPushButton::clicked, view, &VisualCanvas::resetDiagramLayout);
        flowControls->addWidget(resetLayout);
        detailLayout->addLayout(flowControls);
        QTextBrowser *notes = new QTextBrowser(detailPanel);
        // i18n-ignore: stable native identifier for the selected calculation panel.
        notes->setObjectName(QLatin1String("aiVisualFlowNotes"));
        notes->setOpenLinks(false);
        notes->setOpenExternalLinks(false);
        notes->setMinimumHeight(90);
        notes->setPlainText(tr("Select a stage or connection to see how it is calculated."));
        detailLayout->addWidget(notes, 1);
        QToolButton *stageSource = new QToolButton(detailPanel);
        stageSource->setObjectName(QLatin1String("aiVisualStageSource"));
        stageSource->setText(tr("View stage code"));
        stageSource->hide();
        detailLayout->addWidget(stageSource, 0, Qt::AlignRight);
        connect(stageSource, &QToolButton::clicked, selector, [selector, stageSource]() {
          selector->setCurrentIndex(stageSource->property("sourcePage").toInt());
        });
        flowPanels->addWidget(detailPanel);
        flowPanels->setStretchFactor(0, 3);
        flowPanels->setStretchFactor(1, 2);
        flowPanels->setSizes({300, 200});
        connect(notes, &QTextBrowser::anchorClicked, page, [navigate, capture](const QUrl &url) {
          if(navigate && url.scheme() == QLatin1String("rdoc"))
            navigate(url, capture);
        });
        view->diagramSelected = [notes, card, stageSource](int index, int edge) {
          const auto nodes = card.value(QLatin1String("nodes")).toArray();
          QString title, details, calculation;
          QJsonArray links;
          int sourcePage = -1;
          uint32_t eid = uint32_t(card.value(QLatin1String("eventId")).toDouble());
          if(index >= 0 && index < nodes.size())
          {
            const auto node = nodes[index].toObject();
            title = node.value(QLatin1String("text")).toString();
            details = node.value(QLatin1String("details")).toString();
            if(details.isEmpty())
              details = node.value(QLatin1String("description")).toString();
            calculation = node.value(QLatin1String("calculation")).toString();
            links = node.value(QLatin1String("links")).toArray();
            sourcePage = node.value(QLatin1String("codePage")).toInt(-1);
            const uint32_t nodeEid = uint32_t(node.value(QLatin1String("eventId")).toDouble());
            if(nodeEid)
            {
              eid = nodeEid;
              links.append(QJsonObject{
                  {QLatin1String("label"), tr("EID %1").arg(eid)},
                  {QLatin1String("url"), QLatin1String("rdoc://event/") + QString::number(eid)}});
            }
          }
          else if(edge >= 0 && edge < card.value(QLatin1String("edges")).toArray().size())
          {
            const auto labels = card.value(QLatin1String("edgeLabels")).toArray();
            const auto descriptions = card.value(QLatin1String("edgeDetails")).toArray();
            if(edge < labels.size())
              title = labels[edge].toString();
            if(edge < descriptions.size())
              details = descriptions[edge].toString();
            const auto ends = card.value(QLatin1String("edges")).toArray()[edge].toArray();
            if(ends.size() == 2)
              for(const auto &value : nodes)
                if(value.toObject().value(QLatin1String("id")) == ends[1])
                {
                  const auto node = value.toObject();
                  const uint32_t nodeEid = uint32_t(node.value(QLatin1String("eventId")).toDouble());
                  if(nodeEid)
                    eid = nodeEid;
                  if(details.isEmpty())
                    details = node.value(QLatin1String("details")).toString();
                  calculation = node.value(QLatin1String("calculation")).toString();
                  links = node.value(QLatin1String("links")).toArray();
                  sourcePage = node.value(QLatin1String("codePage")).toInt(-1);
                }
          }
          QString html = QLatin1String("<b>") + title.toHtmlEscaped() + QLatin1String("</b>");
          if(!details.isEmpty())
            html += QLatin1String("<p>") + explanationHtml(details, eid) + QLatin1String("</p>");
          if(!calculation.isEmpty())
            html += calculationHtml(calculation, notes->palette());
          QSet<QString> seenLinks;
          for(const auto &value : links)
          {
            const auto link = value.toObject();
            const QUrl url =
                visualResourceLink(QUrl(link.value(QLatin1String("url")).toString()), eid);
            const QString identity = url.toString(QUrl::FullyEncoded);
            if(url.scheme() == QLatin1String("rdoc") && !seenLinks.contains(identity))
            {
              seenLinks.insert(identity);
              html += QLatin1String(" <a href=\"") + identity.toHtmlEscaped() +
                      QLatin1String("\">") +
                      link.value(QLatin1String("label")).toString().toHtmlEscaped() +
                      QLatin1String("</a>");
            }
          }
          stageSource->setProperty("sourcePage", sourcePage);
          stageSource->setVisible(sourcePage >= 0);
          notes->setHtml(html);
          notes->verticalScrollBar()->setValue(0);
        };
      }
    }
    QGridLayout *links = new QGridLayout();
    auto addLink = [this, page, links, navigate, capture](const QString &label, const QUrl &url) {
      if(url.scheme() != QLatin1String("rdoc"))
        return;
      QPushButton *button = new QPushButton(label, page);
      const int count = links->count();
      links->addWidget(button, count / 2, count % 2);
      QObject::connect(button, &QPushButton::clicked, page, [navigate, capture, url]() {
        if(navigate)
          navigate(url, capture);
      });
    };
    QSet<uint32_t> events;
    for(const QJsonObject &source :
        {card, card.value(QLatin1String("experiment")).toObject(),
         card.value(QLatin1String("image")).toObject().value(QLatin1String("source")).toObject(),
         card.value(QLatin1String("secondImage")).toObject().value(QLatin1String("source")).toObject()})
    {
      const uint32_t eid =
          uint32_t(source.value(QLatin1String("eventId"))
                       .toDouble(source.value(QLatin1String("observationEventId")).toDouble()));
      if(eid && !events.contains(eid))
      {
        events.insert(eid);
        addLink(tr("EID %1").arg(eid), QUrl(QLatin1String("rdoc://event/") + QString::number(eid)));
      }
    }
    const QJsonArray extraLinks = card.value(QLatin1String("links")).toArray();
    for(int linkIndex = 0; linkIndex < qMin(6, extraLinks.size()); ++linkIndex)
    {
      const QJsonObject link = extraLinks[linkIndex].toObject();
      addLink(link.value(QLatin1String("label")).toString().left(160),
              visualResourceLink(QUrl(link.value(QLatin1String("url")).toString()),
                                 uint32_t(card.value(QLatin1String("eventId")).toDouble())));
    }
    body->addLayout(links);
    stack->addWidget(page);
  }
  connect(selector, static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged), stack,
          &QStackedWidget::setCurrentIndex);
}

void AIVisualReply::setComparisonMode(int mode)
{
  m_ComparisonMode = mode;
  for(QWidget *widget : findChildren<QWidget *>(QLatin1String("aiVisualImageCanvas")))
  {
    VisualCanvas *view = static_cast<VisualCanvas *>(widget);
    if(view->card.value(QLatin1String("type")).toString() == QLatin1String("compare"))
      view->applyComparisonMode(mode);
  }
}
