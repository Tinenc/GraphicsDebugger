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

#include "AIConversationView.h"
#include "Code/Interface/QRDInterface.h"
#include "AIVisualReply.h"

#include <QAbstractSlider>
#include <QAction>
#include <QApplication>
#include <QBuffer>
#include <QClipboard>
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QCursor>
#include <QDateTime>
#include <QDesktopWidget>
#include <QDir>
#include <QEvent>
#include <QFile>
#include <QFileInfo>
#include <QFocusEvent>
#include <QFontMetrics>
#include <QGridLayout>
#include <QGuiApplication>
#include <QHBoxLayout>
#include <QHideEvent>
#include <QImage>
#include <QImageReader>
#include <QKeyEvent>
#include <QKeySequence>
#include <QLabel>
#include <QLocale>
#include <QMenu>
#include <QMouseEvent>
#include <QPainter>
#include <QPalette>
#include <QPointer>
#include <QRegularExpression>
#include <QScrollBar>
#include <QSet>
#include <QShowEvent>
#include <QTextBlock>
#include <QTextBrowser>
#include <QTextDocument>
#include <QTextFragment>
#include <QTimer>
#include <QToolButton>
#include <QUrlQuery>
#include <QVBoxLayout>
#include <QWheelEvent>
#include <algorithm>
#include <utility>
#include "Code/Resources.h"
#include "AICodeFormatting.h"
#include "AIFileThumbnail.h"

namespace
{
const int ViewportAnchorContextChars = 192;
const int ViewportAnchorCompleteBlockChars = 512;
const int LiveTextRevealIntervalMs = 33;

// Only accepts the simple tag/class selectors emitted by this file. Model text has already
// passed safeMarkdown (which escapes raw HTML). Resolve our stylesheet once, rather than asking
// Qt 5.9's HTML importer to match every class rule against every node on every live update. The
// class-selector path is also where the production transcript crash was observed; this avoids
// that path without treating arbitrary model output as CSS or HTML.
QString inlineTranscriptStyles(const QString &html)
{
  struct Rule
  {
    QString tag;
    QStringList classes;
    QString declarations;
  };
  const int start = html.indexOf(QLatin1String("<style>"));
  const int end = html.indexOf(QLatin1String("</style>"), start);
  if(start < 0 || end < 0)
    return html;

  QVector<Rule> rules;
  const QString css = html.mid(start + 7, end - start - 7);
  const QRegularExpression selectorPattern(
      QLatin1String("^(?:[a-z][a-z0-9]*)?(?:\\.[a-z][a-z0-9-]*)*$"));
  int position = 0;
  while(position < css.size())
  {
    const int open = css.indexOf(QLatin1Char('{'), position);
    const int close = css.indexOf(QLatin1Char('}'), open);
    if(open < 0 || close < 0)
      break;
    const QString declarations = css.mid(open + 1, close - open - 1) + QLatin1Char(';');
    for(const QString &selector : css.mid(position, open - position).split(QLatin1Char(',')))
    {
      const QString normalized = selector.trimmed();
      // A future, more complex first-party selector needs an explicit implementation here.
      // Do not silently change its cascade by partially expanding the stylesheet.
      if(normalized.isEmpty() || !selectorPattern.match(normalized).hasMatch())
        return html;
      Rule rule;
      rule.classes = normalized.split(QLatin1Char('.'));
      rule.tag = rule.classes.takeFirst();
      rule.declarations = declarations;
      rules.push_back(rule);
    }
    position = close + 1;
  }
  std::stable_sort(rules.begin(), rules.end(), [](const Rule &left, const Rule &right) {
    const int leftSpecificity = left.classes.size() * 100 + !left.tag.isEmpty();
    const int rightSpecificity = right.classes.size() * 100 + !right.tag.isEmpty();
    return leftSpecificity < rightSpecificity;
  });

  const QString body = html.left(start) + html.mid(end + 8);
  const QRegularExpression tagPattern(
      QLatin1String("<([a-z][a-z0-9]*)(?:[^<>\"']|\"[^\"]*\"|'[^']*')*>"));
  const QRegularExpression classPattern(QLatin1String("\\sclass=\"([^\"]*)\""));
  const QRegularExpression stylePattern(QLatin1String("\\sstyle=\"([^\"]*)\""));
  QString result;
  result.reserve(body.size() * 2);
  position = 0;
  auto tags = tagPattern.globalMatch(body);
  while(tags.hasNext())
  {
    const QRegularExpressionMatch tag = tags.next();
    result += body.mid(position, tag.capturedStart() - position);
    QString element = tag.captured();
    const auto classAttribute = classPattern.match(element);
    const QStringList classes = classAttribute.captured(1).split(QLatin1Char(' '),
                                                                QString::SkipEmptyParts);
    QString declarations;
    for(const Rule &rule : rules)
    {
      if(!rule.tag.isEmpty() && rule.tag != tag.captured(1))
        continue;
      bool matches = true;
      for(const QString &className : rule.classes)
        matches &= classes.contains(className);
      if(matches)
        declarations += rule.declarations;
    }
    element.remove(classPattern);
    const auto inlineStyle = stylePattern.match(element);
    // Existing inline declarations already are HTML-escaped and have highest specificity.
    const QString style = declarations.toHtmlEscaped() + inlineStyle.captured(1);
    element.remove(stylePattern);
    if(!style.isEmpty())
    {
      const int insertion = element.size() - (element.endsWith(QLatin1String("/>")) ? 2 : 1);
      element.insert(insertion, QLatin1String(" style=\"") + style + QLatin1Char('"'));
    }
    result += element;
    position = tag.capturedEnd();
  }
  result += body.mid(position);
  return result;
}

QString liveTextKey(const AIChatMessage &message, const AIActivityItem &activity)
{
  // i18n-ignore: Unit separators are internal live-stream identity delimiters.
  return message.id + QChar(0x1f) + activity.kind + QChar(0x1f) + activity.id + QChar(0x1f) +
         QString::number(activity.timestampMs);
}

QString liveTextAnchor(const QString &key)
{
  // i18n-ignore: Generated anchor prefix is an internal transcript identifier.
  return lit("replaylab-live-text-") +
         QString::fromLatin1(QCryptographicHash::hash(key.toUtf8(), QCryptographicHash::Sha256)
                                 .toHex());
}

bool sameAttachment(const AIChatAttachment &left, const AIChatAttachment &right)
{
  return left.id == right.id && left.kind == right.kind && left.title == right.title &&
         left.mediaType == right.mediaType && left.source == right.source &&
         left.encoding == right.encoding && left.content == right.content &&
         left.context == right.context && left.originalBytes == right.originalBytes &&
         left.modifiedUnixMs == right.modifiedUnixMs && left.truncated == right.truncated;
}

bool canKeepTranscriptDocument(const QVector<AIChatMessage> &current,
                               const QVector<AIChatMessage> &incoming)
{
  if(current.size() != incoming.size())
    return false;

  for(int messageIndex = 0; messageIndex < current.size(); ++messageIndex)
  {
    const AIChatMessage &oldMessage = current[messageIndex];
    const AIChatMessage &newMessage = incoming[messageIndex];
    if(oldMessage.id != newMessage.id || oldMessage.role != newMessage.role ||
       oldMessage.text != newMessage.text || oldMessage.state != newMessage.state ||
       oldMessage.taskId != newMessage.taskId ||
       oldMessage.provisionalText != newMessage.provisionalText ||
       oldMessage.timestampMs != newMessage.timestampMs ||
       oldMessage.finishedTimestampMs != newMessage.finishedTimestampMs ||
       oldMessage.activityExpanded != newMessage.activityExpanded ||
       oldMessage.visuals != newMessage.visuals ||
       oldMessage.attachments.size() != newMessage.attachments.size() ||
       oldMessage.activity.size() != newMessage.activity.size())
      return false;

    for(int attachmentIndex = 0; attachmentIndex < oldMessage.attachments.size(); ++attachmentIndex)
      if(!sameAttachment(oldMessage.attachments[attachmentIndex],
                         newMessage.attachments[attachmentIndex]))
        return false;

    for(int activityIndex = 0; activityIndex < oldMessage.activity.size(); ++activityIndex)
    {
      const AIActivityItem &oldActivity = oldMessage.activity[activityIndex];
      const AIActivityItem &newActivity = newMessage.activity[activityIndex];
      if(oldActivity.id != newActivity.id || oldActivity.kind != newActivity.kind ||
         oldActivity.title != newActivity.title ||
         oldActivity.timestampMs != newActivity.timestampMs ||
         oldActivity.finishedTimestampMs != newActivity.finishedTimestampMs ||
         oldActivity.completed != newActivity.completed || oldActivity.error != newActivity.error ||
         oldActivity.liveText != newActivity.liveText ||
         oldActivity.exactText != newActivity.exactText ||
         oldActivity.liveTextRevision != newActivity.liveTextRevision)
        return false;

      if(oldActivity.liveText)
      {
        // A same-revision prefix extension changes only the live target. Its existing document
        // cursor can reveal the suffix without replacing QTextDocument, preserving selections and
        // the user's viewport.
        if(!newActivity.detail.startsWith(oldActivity.detail))
          return false;
      }
      else if(oldActivity.detail != newActivity.detail)
      {
        return false;
      }
    }
  }
  return true;
}

struct MessageDocumentAnchor
{
  QString messageId;
  int position = 0;
};

QVector<MessageDocumentAnchor> messageDocumentAnchors(
    QTextDocument *document, const QHash<QString, QString> &messageAnchors)
{
  QVector<MessageDocumentAnchor> positions;
  if(!document || messageAnchors.isEmpty())
    return positions;

  QHash<QString, QString> messageIdByAnchor;
  for(auto it = messageAnchors.constBegin(); it != messageAnchors.constEnd(); ++it)
    messageIdByAnchor[it.value()] = it.key();

  for(QTextBlock block = document->begin(); block.isValid(); block = block.next())
  {
    for(QTextBlock::iterator it = block.begin(); !it.atEnd(); ++it)
    {
      const QTextFragment fragment = it.fragment();
      if(!fragment.isValid())
        continue;
      const QStringList names = fragment.charFormat().anchorNames();
      for(const QString &name : names)
      {
        const auto found = messageIdByAnchor.constFind(name);
        if(found == messageIdByAnchor.constEnd())
          continue;
        MessageDocumentAnchor position;
        position.messageId = found.value();
        position.position = fragment.position();
        positions.push_back(position);
      }
    }
  }
  return positions;
}

bool allowedLink(const QUrl &url)
{
  if(!url.isValid())
    return false;
  const QString scheme = url.scheme().toLower();
  return scheme == lit("http") || scheme == lit("https") || scheme == lit("rdoc") || // i18n-ignore: URL scheme identifiers.
         scheme == lit("replaylab-artifact"); // i18n-ignore: Local artifact URL scheme identifier.
}

QUrl markdownNavigationTarget(QString target)
{
  // Source citations are navigation requests, never shell-open requests. The workspace controller
  // resolves the exact absolute path against its authorised documents when the user clicks.
  target.replace(QLatin1Char('\\'), QLatin1Char('/'));
  if(target.startsWith(QLatin1String("file:")))
    target = QUrl(target).toLocalFile();
  static const QRegularExpression sourceCitation(
      QStringLiteral(R"(^((?:[A-Za-z]:/|/)[^\r\n]*\.(?:hlsl|hlsli|glsl|glslinc|slang|slangh|vert|frag|geom|comp|tesc|tese|fx|fxh|h|inc|usf|ush))(?::([1-9][0-9]{0,8})(?::([1-9][0-9]{0,8}))?|#L([1-9][0-9]{0,8})(?:C([1-9][0-9]{0,8}))?)?$)"),
      QRegularExpression::CaseInsensitiveOption);
  const QRegularExpressionMatch match = sourceCitation.match(target);
  if(match.hasMatch())
  {
    QUrl source(QStringLiteral("rdoc://shader"));
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("path"), match.captured(1));
    const QString line = match.captured(2).isEmpty() ? match.captured(4) : match.captured(2);
    const QString column = match.captured(3).isEmpty() ? match.captured(5) : match.captured(3);
    if(!line.isEmpty())
      query.addQueryItem(QStringLiteral("line"), line);
    if(!column.isEmpty())
      query.addQueryItem(QStringLiteral("column"), column);
    source.setQuery(query);
    return source;
  }
  static const QRegularExpression localPath(QStringLiteral(R"(^(?:[A-Za-z]:/|/|//)[^\r\n]+$)"));
  static const QRegularExpression executablePath(QStringLiteral(R"(\.(?:exe|com|cmd|bat|msi|scr|lnk|url|vbs|jse|wsf|reg)$)"),
                                                QRegularExpression::CaseInsensitiveOption);
  if(localPath.match(target).hasMatch() && executablePath.match(target).hasMatch())
    return QUrl();
  if(localPath.match(target).hasMatch())
    return QUrl(lit("replaylab-artifact://open/") + // i18n-ignore: Local artifact navigation URI.
                QString::fromUtf8(QUrl::toPercentEncoding(target)));
  return QUrl(target, QUrl::StrictMode);
}

// Render inline code and [label](target) links in one forward pass. A nested '[' invalidates the
// current link candidate, which keeps malformed model output from causing repeated rescans.
QString inlineMarkdown(const QString &text)
{
  QString html;
  html.reserve(text.size() + text.size() / 8);
  int plainStart = 0;
  int i = 0;
  while(i < text.size())
  {
    if(i + 1 < text.size() && text[i] == QLatin1Char('*') && text[i + 1] == QLatin1Char('*'))
    {
      const int end = text.indexOf(QLatin1String("**"), i + 2);
      if(end > i + 2)
      {
        html += text.mid(plainStart, i - plainStart).toHtmlEscaped();
        html += QLatin1String("<strong>") + inlineMarkdown(text.mid(i + 2, end - i - 2)) +
                QLatin1String("</strong>");
        i = plainStart = end + 2;
        continue;
      }
    }
    if(text[i] == QLatin1Char('`'))
    {
      const int end = text.indexOf(QLatin1Char('`'), i + 1);
      if(end < 0)
        break;
      html += text.mid(plainStart, i - plainStart).toHtmlEscaped();
      const QString code = text.mid(i + 1, end - i - 1);
      const QUrl path = markdownNavigationTarget(code);
      if(path.scheme() == QLatin1String("replaylab-artifact"))
        html += lit("<a href=\"") + path.toString(QUrl::FullyEncoded).toHtmlEscaped() + lit("\"><code>") + code.toHtmlEscaped() + lit("</code></a>"); // i18n-ignore: HTML link/code markup with escaped user path.
      else
        html += lit("<code>") + code.toHtmlEscaped() + lit("</code>"); // i18n-ignore: HTML code markup with escaped user content.
      i = end + 1;
      plainStart = i;
      continue;
    }

    if(text[i] == QLatin1Char('['))
    {
      int labelEnd = -1;
      for(int j = i + 1; j < text.size(); ++j)
      {
        if(text[j] == QLatin1Char('[') || text[j] == QLatin1Char('\n') ||
           text[j] == QLatin1Char('\r'))
          break;
        if(text[j] == QLatin1Char(']'))
        {
          labelEnd = j;
          break;
        }
      }

      if(labelEnd > i + 1 && labelEnd + 1 < text.size() && text[labelEnd + 1] == QLatin1Char('('))
      {
        int targetEnd = -1;
        int targetStart = labelEnd + 2;
        const bool angleTarget = targetStart < text.size() && text[targetStart] == QLatin1Char('<');
        if(angleTarget)
          ++targetStart;
        for(int j = targetStart; j < text.size(); ++j)
        {
          if((!angleTarget && text[j] == QLatin1Char(')')) ||
             (angleTarget && text[j] == QLatin1Char('>') && j + 1 < text.size() &&
              text[j + 1] == QLatin1Char(')')))
          {
            targetEnd = j;
            break;
          }
          if(text[j] == QLatin1Char('\n') || text[j] == QLatin1Char('\r') ||
             (!angleTarget && (text[j].isSpace() || text[j] == QLatin1Char('('))))
            break;
        }

        if(targetEnd > targetStart)
        {
          const QUrl target = markdownNavigationTarget(text.mid(targetStart, targetEnd - targetStart));
          if(allowedLink(target))
          {
            html += text.mid(plainStart, i - plainStart).toHtmlEscaped();
            html += lit("<a href=\"") + target.toString(QUrl::FullyEncoded).toHtmlEscaped() +
                    lit("\">") + text.mid(i + 1, labelEnd - i - 1).toHtmlEscaped() + lit("</a>");
            i = targetEnd + (angleTarget ? 2 : 1);
            plainStart = i;
            continue;
          }
        }
      }
    }
    ++i;
  }
  html += text.mid(plainStart).toHtmlEscaped();
  return html;
}

void closeList(QString &html, int &listKind)
{
  if(listKind == 1)
    html += lit("</ul>");
  else if(listKind == 2)
    html += lit("</ol>");
  listKind = 0;
}

bool orderedListLine(const QString &line, int &contentStart)
{
  int i = 0;
  while(i < line.size() && line[i].isDigit())
    ++i;
  if(i == 0 || i > 9 || i + 1 >= line.size() || line[i] != QLatin1Char('.') || !line[i + 1].isSpace())
    return false;
  contentStart = i + 2;
  return true;
}

using AICodeFormatting::highlightedSourceLine;
using AICodeFormatting::sourceType;

QString renderFencedCode(const QStringList &lines, QString language)
{
  const QRegularExpression binding(QStringLiteral("(?:^|\\s)source=(rdoc://shader/[^\\s]+)"));
  const QRegularExpressionMatch match = binding.match(language);
  QUrl source = match.hasMatch() ? QUrl(match.captured(1), QUrl::StrictMode) : QUrl();
  if(source.host() != QLatin1String("shader") || source.scheme() != QLatin1String("rdoc"))
    source = QUrl();
  const QUrlQuery sourceQuery(source);
  const int startLine = qMax(1, sourceQuery.queryItemValue(QLatin1String("line")).toInt());
  language = language.trimmed().section(QLatin1Char(' '), 0, 0).left(32);
  QString label = language;
  if(label.isEmpty())
    label = QCoreApplication::translate("AIConversationView", "Code");

  QString html = QLatin1String("<table class=\"source-block\" width=\"100%\" border=\"0\" ") +
                 QLatin1String("cellspacing=\"0\" cellpadding=\"0\"><tr><td colspan=\"2\" ") +
                 QLatin1String("class=\"source-header\">") + label.toHtmlEscaped() +
                 QLatin1String("</td></tr>");
  const int lineCount = qMax(1, lines.size());
  bool blockComment = false;
  for(int lineIndex = 0; lineIndex < lineCount; ++lineIndex)
  {
    const QString line = lineIndex < lines.size() ? lines[lineIndex] : QString();
    QString rendered = line.isEmpty() ? QLatin1String("&nbsp;")
                                      : highlightedSourceLine(line, language, blockComment);
    QString number = QString::number(lineIndex + 1);
    if(!source.isEmpty())
    {
      QUrl jump = source;
      QUrlQuery query = sourceQuery;
      query.removeAllQueryItems(QLatin1String("line"));
      query.addQueryItem(QLatin1String("line"), QString::number(startLine));
      jump.setQuery(query);
      const QString prefix = QLatin1String("<a href=\"") +
                             jump.toString(QUrl::FullyEncoded).toHtmlEscaped() +
                             QLatin1String("\">");
      rendered = prefix + rendered + QLatin1String("</a>");
      number = prefix + number + QLatin1String("</a>");
    }
    html += QLatin1String("<tr><td class=\"source-line-number\" width=\"36\" align=\"right\" ") +
            QLatin1String("valign=\"top\">") + number +
            QLatin1String("</td><td class=\"source-line\" valign=\"top\">") + rendered +
            QLatin1String("</td></tr>");
  }
  return html + QLatin1String("</table>");
}

// Respect escaped separators and pipes inside inline code.
QStringList markdownTableCells(const QString &line)
{
  QStringList cells;
  const QString trimmed = line.trimmed();
  if(!trimmed.startsWith(QLatin1Char('|')))
    return cells;
  bool code = false;
  QString cell;
  for(int i = 1; i < trimmed.size(); ++i)
  {
    const QChar ch = trimmed[i];
    if(ch == QLatin1Char('\\') && i + 1 < trimmed.size() && trimmed[i + 1] == QLatin1Char('|'))
      cell += trimmed[++i];
    else if(ch == QLatin1Char('|') && !code)
    {
      cells.push_back(cell.trimmed());
      cell.clear();
    }
    else
    {
      if(ch == QLatin1Char('`'))
        code = !code;
      cell += ch;
    }
  }
  if(!cell.isEmpty())
    cells.push_back(cell.trimmed());
  return cells;
}

bool markdownTableRule(const QStringList &cells, int columns)
{
  if(cells.size() != columns || columns < 2 || columns > 12)
    return false;
  static const QRegularExpression separator(QStringLiteral("^:?-{1,}:?$"));
  for(const QString &cell : cells)
    if(!separator.match(cell).hasMatch())
      return false;
  return true;
}

// Raw HTML and images are intentionally unsupported. This parser is small, deterministic, and
// linear for the bounded transcript sizes used by AIAssistant.
QString safeMarkdown(QString text)
{
  text.replace(lit("\r\n"), lit("\n"));
  text.replace(QLatin1Char('\r'), QLatin1Char('\n'));
  const QStringList lines = text.split(QLatin1Char('\n'), QString::KeepEmptyParts);
  QString html;
  html.reserve(text.size() + text.size() / 4);
  bool codeBlock = false;
  bool codeInList = false;
  QString codeLanguage;
  QStringList codeLines;
  int listKind = 0;
  bool listItemOpen = false;
  auto closeLists = [&]() {
    if(listItemOpen)
      html += lit("</li>");    // i18n-ignore: closing HTML list-item tag.
    listItemOpen = false;
    closeList(html, listKind);
  };

  for(int row = 0; row < lines.size(); ++row)
  {
    const QString &line = lines[row];
    const QString trimmed = line.trimmed();
    if(trimmed.startsWith(lit("```")))
    {
      if(!codeBlock)
        codeInList =
            listItemOpen && (line.startsWith(lit("  ")) || line.startsWith(QLatin1Char('\t')));
      if(!codeInList)
        closeLists();
      if(codeBlock)
      {
        html += renderFencedCode(codeLines, codeLanguage);
        codeLines.clear();
        codeLanguage.clear();
        codeInList = false;
      }
      else
      {
        codeLanguage = trimmed.mid(3).trimmed();
      }
      codeBlock = !codeBlock;
      continue;
    }
    if(codeBlock)
    {
      codeLines.push_back(line);
      continue;
    }
    if(trimmed.isEmpty())
    {
      int next = row + 1;
      while(next < lines.size() && lines[next].trimmed().isEmpty())
        ++next;
      row = next - 1;
      int orderedStart = 0;
      const bool continues =
          listKind && next < lines.size() &&
          (lines[next].startsWith(lit("  ")) || lines[next].startsWith(QLatin1Char('\t')) ||
           (lines[next].trimmed().startsWith(QLatin1Char('[')) &&
            lines[next].trimmed().endsWith(QLatin1Char(')'))) ||
           (listKind == 2 && orderedListLine(lines[next], orderedStart)) ||
           (listKind == 1 &&
            (lines[next].startsWith(lit("- ")) || lines[next].startsWith(lit("* ")))));
      if(!continues)
        closeLists();
      continue;
    }

    const QStringList cells = markdownTableCells(line);
    if(row + 1 < lines.size() && markdownTableRule(markdownTableCells(lines[row + 1]), cells.size()))
    {
      closeLists();
      html += QLatin1String("<table border=\"1\" cellspacing=\"0\" cellpadding=\"4\"><tr>");
      for(const QString &cell : cells)
        html += QLatin1String("<th>") + inlineMarkdown(cell) + QLatin1String("</th>");
      html += QLatin1String("</tr>");
      ++row;
      while(row + 1 < lines.size())
      {
        const QStringList values = markdownTableCells(lines[row + 1]);
        if(values.size() != cells.size())
          break;
        ++row;
        html += QLatin1String("<tr>");
        for(const QString &value : values)
          html += QLatin1String("<td>") + inlineMarkdown(value) + QLatin1String("</td>");
        html += QLatin1String("</tr>");
      }
      html += QLatin1String("</table>");
      continue;
    }

    int heading = 0;
    while(heading < line.size() && heading < 4 && line[heading] == QLatin1Char('#'))
      ++heading;
    if(heading > 0 && heading < line.size() && line[heading].isSpace())
    {
      closeLists();
      html += lit("<h") + QString::number(heading + 2) + lit(">") +
              inlineMarkdown(line.mid(heading + 1)) + lit("</h") + QString::number(heading + 2) +
              lit(">");
      continue;
    }

    if(line.startsWith(lit("- ")) || line.startsWith(lit("* ")))
    {
      if(listKind != 1)
      {
        closeLists();
        html += lit("<ul>");
        listKind = 1;
      }
      if(listItemOpen)
        html += lit("</li>");    // i18n-ignore: closing HTML list-item tag.
      // i18n-ignore: opening HTML list-item tag, no user-visible prose.
      html += lit("<li>") + inlineMarkdown(line.mid(2));
      listItemOpen = true;
      continue;
    }

    int orderedStart = 0;
    if(orderedListLine(line, orderedStart))
    {
      if(listKind != 2)
      {
        closeLists();
        html += lit("<ol>");
        listKind = 2;
      }
      if(listItemOpen)
        html += lit("</li>");    // i18n-ignore: closing HTML list-item tag.
      // i18n-ignore: opening HTML list-item tag, no user-visible prose.
      html += lit("<li>") + inlineMarkdown(line.mid(orderedStart));
      listItemOpen = true;
      continue;
    }

    if(listItemOpen && !line.startsWith(lit("> ")))
    {
      // A Qt 5 list item is one QTextBlock. Line breaks keep explanatory text
      // and source links in that item instead of restarting the next number.
      const bool citation =
          trimmed.startsWith(QLatin1Char('[')) && trimmed.endsWith(QLatin1Char(')'));
      // i18n-ignore: whitespace and structural HTML line break, no prose.
      html += (citation ? lit(" ") : lit("<br />")) + inlineMarkdown(trimmed);
      continue;
    }
    closeLists();
    if(line.startsWith(lit("> ")))
      html += lit("<blockquote>") + inlineMarkdown(line.mid(2)) + lit("</blockquote>");
    else
      html += lit("<p>") + inlineMarkdown(line) + lit("</p>");
  }

  closeLists();
  if(codeBlock)
    html += renderFencedCode(codeLines, codeLanguage);
  return html;
}

QString activityState(const AIActivityItem &activity)
{
  if(activity.kind == lit("queued"))
    return QCoreApplication::translate("AIConversationView", "queued");
  if(activity.kind == lit("cancelled"))
    return QCoreApplication::translate("AIConversationView", "cancelled");
  if(activity.kind == lit("interrupted"))
    return QCoreApplication::translate("AIConversationView", "interrupted");
  if(activity.kind == lit("superseded"))
    return QCoreApplication::translate("AIConversationView", "redirected");
  if(activity.kind == lit("steered"))
    return QCoreApplication::translate("AIConversationView", "adjusted");
  if(activity.kind == lit("approval"))
    return QCoreApplication::translate("AIConversationView", "attention");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(activity.kind == lit("needs-user-input"))
    return QCoreApplication::translate("AIConversationView", "waiting for input");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(activity.kind == lit("blocked"))
    return QCoreApplication::translate("AIConversationView", "blocked");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(activity.kind == lit("partial"))
    return QCoreApplication::translate("AIConversationView", "turn ended with unfinished work");
  if(activity.error)
    return QCoreApplication::translate("AIConversationView", "failed");
  // i18n-ignore: Activity kinds are backend protocol identifiers used only for label selection.
  if(activity.kind == lit("completed") || activity.kind == lit("main") ||
     // i18n-ignore: Child activity-kind prefix is a backend protocol identifier.
     activity.kind.startsWith(lit("child-")))
    return activity.completed
               ? QCoreApplication::translate("AIConversationView", "turn ended")
               : QCoreApplication::translate("AIConversationView", "running");
  if(activity.completed)
    return QCoreApplication::translate("AIConversationView", "completed");
  return QCoreApplication::translate("AIConversationView", "running");
}

QString messageStateLabel(const QString &state)
{
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("queued"))
    return QCoreApplication::translate("AIConversationView", "queued");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("running"))
    return QCoreApplication::translate("AIConversationView", "running");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("planning"))
    return QCoreApplication::translate("AIConversationView", "planning");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("finalizing"))
    return QCoreApplication::translate("AIConversationView", "finalizing");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("cancelled"))
    return QCoreApplication::translate("AIConversationView", "cancelled");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("interrupted"))
    return QCoreApplication::translate("AIConversationView", "interrupted");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("superseded"))
    return QCoreApplication::translate("AIConversationView", "redirected");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("steered"))
    return QCoreApplication::translate("AIConversationView", "adjusted");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("failed"))
    return QCoreApplication::translate("AIConversationView", "failed");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("completed"))
    return QCoreApplication::translate("AIConversationView", "turn ended");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("partial"))
    return QCoreApplication::translate("AIConversationView", "turn ended with unfinished work");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("needs-user-input"))
    return QCoreApplication::translate("AIConversationView", "waiting for input");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("blocked"))
    return QCoreApplication::translate("AIConversationView", "blocked");
  return state;
}

bool isLiveState(const QString &state)
{
  // i18n-ignore: Conversation states are internal lifecycle identifiers used only for control flow.
  return state == lit("queued") || state == lit("running") || state == lit("finalizing");
}

QString elapsedText(qint64 elapsedMs)
{
  const qint64 totalSeconds = qMax<qint64>(0, elapsedMs / 1000);
  const qint64 hours = totalSeconds / 3600;
  const int minutes = int((totalSeconds % 3600) / 60);
  const int seconds = int(totalSeconds % 60);

  if(hours > 0)
    return QCoreApplication::translate("AIConversationView", "%1 h %2 min %3 sec")
        .arg(hours)
        .arg(minutes)
        .arg(seconds);
  if(minutes > 0)
    return QCoreApplication::translate("AIConversationView", "%1 min %2 sec")
        .arg(minutes)
        .arg(seconds);
  return QCoreApplication::translate("AIConversationView", "%1 sec").arg(seconds);
}

QString activityKindLabel(const QString &kind)
{
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("tool"))
    return QCoreApplication::translate("AIConversationView", "Tool");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("main"))
    return QCoreApplication::translate("AIConversationView", "Main Agent");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("reasoning"))
    return QCoreApplication::translate("AIConversationView", "Reasoning summary");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("reasoning-raw"))
    return QCoreApplication::translate("AIConversationView", "Raw reasoning");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("commandExecution"))
    return QCoreApplication::translate("AIConversationView", "Command");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("mcpToolCall"))
    return QCoreApplication::translate("AIConversationView", "MCP tool");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("webSearch"))
    return QCoreApplication::translate("AIConversationView", "Web search");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("imageView"))
    return QCoreApplication::translate("AIConversationView", "Image");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("contextCompaction"))
    return QCoreApplication::translate("AIConversationView", "Context");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("providerStatus"))
    return QCoreApplication::translate("AIConversationView", "Provider");
  // i18n-ignore: Activity kind prefix is a backend protocol identifier used for label selection.
  if(kind.startsWith(lit("child-")))
    return QCoreApplication::translate("AIConversationView", "Agent");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("approval"))
    return QCoreApplication::translate("AIConversationView", "Approval");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("needs-user-input"))
    return QCoreApplication::translate("AIConversationView", "Input needed");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("blocked"))
    return QCoreApplication::translate("AIConversationView", "Blocked");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("completed"))
    return QCoreApplication::translate("AIConversationView", "Turn ended");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("partial"))
    return QCoreApplication::translate("AIConversationView", "Turn ended with unfinished work");
  // i18n-ignore: Activity kinds are backend protocol identifiers used only for label selection.
  if(kind == lit("steered") || kind == lit("superseded"))
    return QCoreApplication::translate("AIConversationView", "Direction");
  return kind.isEmpty() ? QCoreApplication::translate("AIConversationView", "Activity") : kind;
}

QString activityStatusClass(const AIActivityItem &activity)
{
  if(activity.error)
    // i18n-ignore: CSS class name is an internal selector, not user-visible text.
    return lit("activity-error");
  if(activity.completed)
    // i18n-ignore: CSS class name is an internal selector, not user-visible text.
    return lit("activity-completed");
  // i18n-ignore: CSS class name is an internal selector, not user-visible text.
  return lit("activity-running");
}

bool isCodeActivity(const QString &kind, const QString &detail)
{
  // i18n-ignore: Activity kinds are backend protocol identifiers used only for styling.
  if(kind == QLatin1String("commandExecution") || kind == QLatin1String("mcpToolCall"))
    return true;
  // Generic tools often report only a one-word lifecycle status; leave that in the timeline as
  // prose. Promote actual arguments, results, or multi-line payloads into a code surface.
  return kind == QLatin1String("tool") &&
         (detail.contains(QLatin1Char('\n')) || detail.contains(QLatin1Char('{')) ||
          detail.contains(QLatin1Char('[')));
}

QString highlightedDetailLine(const QString &line)
{
  QString html;
  int i = 0;
  while(i < line.size())
  {
    if(i + 1 < line.size() && line[i] == QLatin1Char('/') &&
       line[i + 1] == QLatin1Char('/'))
    {
      html += QLatin1String("<span class=\"detail-comment\">") +
              line.mid(i).toHtmlEscaped() + QLatin1String("</span>");
      break;
    }
    if(i + 1 < line.size() && line[i] == QLatin1Char('/') &&
       line[i + 1] == QLatin1Char('*'))
    {
      const int end = line.indexOf(QLatin1String("*/"), i + 2);
      const int length = end < 0 ? line.size() - i : end + 2 - i;
      html += QLatin1String("<span class=\"detail-comment\">") +
              line.mid(i, length).toHtmlEscaped() + QLatin1String("</span>");
      i += length;
      continue;
    }
    if(line[i] == QLatin1Char('#') &&
       (i == 0 || line[i - 1].isSpace()))
    {
      html += QLatin1String("<span class=\"detail-comment\">") +
              line.mid(i).toHtmlEscaped() + QLatin1String("</span>");
      break;
    }
    if(line[i] == QLatin1Char('"') || line[i] == QLatin1Char('\''))
    {
      const QChar quote = line[i];
      const int start = i++;
      bool escaped = false;
      while(i < line.size())
      {
        const QChar ch = line[i++];
        if(ch == quote && !escaped)
          break;
        if(ch == QLatin1Char('\\') && !escaped)
          escaped = true;
        else
          escaped = false;
      }
      int next = i;
      while(next < line.size() && line[next].isSpace())
        ++next;
      const QString cssClass = next < line.size() && line[next] == QLatin1Char(':')
                                   ? QLatin1String("detail-key")
                                   : QLatin1String("detail-string");
      html += QLatin1String("<span class=\"") + cssClass + QLatin1String("\">") +
              line.mid(start, i - start).toHtmlEscaped() + QLatin1String("</span>");
      continue;
    }

    if(line[i].isLetter() || line[i] == QLatin1Char('_'))
    {
      const int start = i++;
      while(i < line.size() &&
            (line[i].isLetterOrNumber() || line[i] == QLatin1Char('_') ||
             line[i] == QLatin1Char('-')))
        ++i;
      int next = i;
      while(next < line.size() && line[next].isSpace())
        ++next;
      const QString word = line.mid(start, i - start);
      QString cssClass;
      if(next < line.size() && line[next] == QLatin1Char(':'))
        // i18n-ignore: CSS class name is an internal selector.
        cssClass = QLatin1String("detail-key");
      else if(word.compare(QLatin1String("true"), Qt::CaseInsensitive) == 0 ||
              word.compare(QLatin1String("false"), Qt::CaseInsensitive) == 0 ||
              word.compare(QLatin1String("null"), Qt::CaseInsensitive) == 0)
        // i18n-ignore: CSS class name is an internal selector.
        cssClass = QLatin1String("detail-literal");
      else if(word.compare(QLatin1String("running"), Qt::CaseInsensitive) == 0 ||
              word.compare(QLatin1String("completed"), Qt::CaseInsensitive) == 0 ||
              word.compare(QLatin1String("failed"), Qt::CaseInsensitive) == 0 ||
              word.compare(QLatin1String("error"), Qt::CaseInsensitive) == 0)
        // i18n-ignore: CSS class name is an internal selector.
        cssClass = QLatin1String("detail-status");
      else if(next < line.size() && line[next] == QLatin1Char('('))
        // i18n-ignore: CSS class name is an internal selector.
        cssClass = QLatin1String("detail-function");
      else if(sourceType(word))
        // i18n-ignore: CSS class name is an internal selector.
        cssClass = QLatin1String("detail-type");
      else
        // i18n-ignore: CSS class name is an internal selector.
        cssClass = QLatin1String("detail-variable");

      html += QLatin1String("<span class=\"") + cssClass + QLatin1String("\">") +
              word.toHtmlEscaped() + QLatin1String("</span>");
      continue;
    }

    if(line[i].isDigit() ||
       (line[i] == QLatin1Char('-') && i + 1 < line.size() && line[i + 1].isDigit()))
    {
      const int start = i++;
      while(i < line.size() &&
            (line[i].isDigit() || line[i] == QLatin1Char('.') ||
             line[i] == QLatin1Char('e') || line[i] == QLatin1Char('E') ||
             line[i] == QLatin1Char('+') || line[i] == QLatin1Char('-')))
        ++i;
      html += QLatin1String("<span class=\"detail-number\">") +
              line.mid(start, i - start).toHtmlEscaped() + QLatin1String("</span>");
      continue;
    }

    html += QString(line[i]).toHtmlEscaped();
    ++i;
  }
  return html;
}

QString renderActivityDetail(QString detail, bool codeStyle)
{
  detail.replace(lit("\r\n"), lit("\n"));
  detail.replace(QLatin1Char('\r'), QLatin1Char('\n'));
  QStringList lines = detail.split(QLatin1Char('\n'), QString::KeepEmptyParts);
  while(!lines.isEmpty() && lines.first().trimmed().isEmpty())
    lines.removeFirst();
  while(!lines.isEmpty() && lines.last().trimmed().isEmpty())
    lines.removeLast();
  if(lines.isEmpty())
    return QString();

  if(codeStyle)
  {
    const int maxLines = 10;
    const int maxLineCharacters = 280;
    const int visibleLineCount = qMin(lines.size(), maxLines);
    bool truncated = lines.size() > maxLines;
    // Use a small table instead of CSS overflow/max-height: those properties are not reliably
    // honoured by Qt 5.9's QTextDocument. Capping rows gives tool output a predictable footprint.
    // i18n-ignore: Structural transcript HTML contains no user-visible prose.
    QString html = QLatin1String("<table class=\"activity-code\" width=\"100%\" border=\"0\" ") +
                   // i18n-ignore: Structural transcript HTML contains no user-visible prose.
                   QLatin1String("cellspacing=\"0\" cellpadding=\"0\">");
    for(int lineIndex = 0; lineIndex < visibleLineCount; ++lineIndex)
    {
      QString line = lines[lineIndex];
      if(line.size() > maxLineCharacters)
      {
        line = line.left(maxLineCharacters) + QChar(0x2026);
        truncated = true;
      }
      // i18n-ignore: Structural transcript HTML contains no user-visible prose.
      html += QLatin1String("<tr><td class=\"code-line-number\" width=\"30\" align=\"right\" ") +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              QLatin1String("valign=\"top\">") + QString::number(lineIndex + 1) +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              QLatin1String("</td><td class=\"code-line\" valign=\"top\">") +
              (line.isEmpty() ? QLatin1String("&nbsp;") : highlightedDetailLine(line)) +
              QLatin1String("</td></tr>");
    }
    if(truncated)
    {
      const QString omitted = QCoreApplication::translate(
          "AIConversationView", "Additional output omitted from the timeline");
      // i18n-ignore: Structural transcript HTML contains no user-visible prose.
      html += QLatin1String("<tr><td class=\"code-line-number\"></td><td class=\"code-omitted\">") +
              omitted.toHtmlEscaped() + QLatin1String("&hellip;</td></tr>");
    }
    // i18n-ignore: Structural transcript HTML contains no user-visible prose.
    return html + QLatin1String("</table>");
  }

  QStringList visibleLines;
  visibleLines.reserve(lines.size());
  for(const QString &line : lines)
    visibleLines.push_back(line.toHtmlEscaped());
  QString html = visibleLines.join(QLatin1String("<br/>"));
  return QLatin1String("<div class=\"activity-detail\">") + html + QLatin1String("</div>");
}

QString renderLiveActivityDetail(const AIChatMessage &message, const AIActivityItem &activity,
                                 QHash<QString, QString> *liveTextAnchors)
{
  const QString key = liveTextKey(message, activity);
  const QString anchor = liveTextAnchor(key);
  if(liveTextAnchors)
    liveTextAnchors->insert(key, anchor);
  const int visibleChars = activity.visibleDetailChars < 0
                               ? activity.detail.size()
                               : qBound(0, activity.visibleDetailChars, activity.detail.size());
  const QString visible = activity.detail.left(visibleChars);
  // QTextCursor inserts later graphemes as plain text immediately before this named tail anchor.
  // Escaping the initial prefix preserves the exact stream without interpreting JSON, Markdown,
  // links, HTML, or code fences.
  // i18n-ignore: Structural transcript HTML and zero-width marker contain no user-visible prose.
  return lit("<div class=\"activity-detail activity-live-text\">") + visible.toHtmlEscaped() +
         lit("<span class=\"activity-live-tail\" id=\"") + anchor.toHtmlEscaped() +    // i18n-ignore: structural transcript HTML
         lit("\">&#8203;</span></div>");    // i18n-ignore: structural transcript HTML and zero-width marker
}

QString renderActivityGroup(const AIChatMessage &message, qint64 finishedMs, qint64 nowMs,
                            QHash<QString, QString> *liveTextAnchors,
                            QHash<QString, QPair<qint64, QString>> *elapsedAnchors = NULL,
                            int maxActivities = 96)
{
  if(message.activity.isEmpty())
    return QString();

  const QString elapsedToken = QString::fromLatin1(QUrl::toPercentEncoding(message.id));
  qint64 effectiveFinishedMs = finishedMs;
  bool groupRunning = false;
  if(isLiveState(message.state))
  {
    effectiveFinishedMs = nowMs;
    groupRunning = true;
  }
  else if(effectiveFinishedMs <= 0)
  {
    for(const AIActivityItem &activity : message.activity)
      effectiveFinishedMs = qMax(effectiveFinishedMs, activity.finishedTimestampMs);
  }

  QString duration = lit("--");
  if(message.timestampMs > 0 && effectiveFinishedMs >= message.timestampMs)
    duration = elapsedText(effectiveFinishedMs - message.timestampMs);

  const QString token = QString::fromLatin1(QUrl::toPercentEncoding(message.id));
  const bool nextExpanded = !message.activityExpanded;
  const QString href = lit("replaylab-activity://toggle/") + token + lit("?expanded=") +
                       (nextExpanded ? lit("1") : lit("0"));
  // A running group's duration is a live label: the anchor name lets the view
  // update just those characters every second.
  QString durationHtml = duration.toHtmlEscaped();
  if(groupRunning && message.timestampMs > 0 && elapsedAnchors != NULL)
  {
    const QString anchorName = lit("elapsed-g-") + elapsedToken;    // i18n-ignore: internal anchor id.
    elapsedAnchors->insert(anchorName, qMakePair(message.timestampMs, duration));
    // i18n-ignore: Structural transcript HTML contains no user-visible prose.
    durationHtml = lit("<a name=\"") + anchorName + lit("\">") + duration.toHtmlEscaped() + lit("</a>");
  }
  const QString elapsed =
      QCoreApplication::translate("AIConversationView", "Elapsed %1").arg(lit("\x01"));

  // i18n-ignore: Structural transcript HTML contains no user-visible prose.
  QString html = lit("<div class=\"activity-group\"><table class=\"activity-header\" ") +
                 // i18n-ignore: Structural transcript HTML contains no user-visible prose.
                 lit("width=\"100%\" border=\"0\" cellspacing=\"0\" cellpadding=\"0\">") +
                 // i18n-ignore: Structural transcript HTML contains no user-visible prose.
                 lit("<tr><td width=\"18\" valign=\"middle\"><a class=\"activity-disclosure\" href=\"") +
                 href.toHtmlEscaped() + lit("\">") +
                 // i18n-ignore: HTML entities are visual disclosure indicators, not prose.
                 (message.activityExpanded ? lit("&#9660;") : lit("&#9654;")) +
                 // i18n-ignore: Structural transcript HTML contains no user-visible prose.
                 lit("</a></td><td><a class=\"activity-toggle\" href=\"") +
                 href.toHtmlEscaped() + lit("\">") +
                 elapsed.toHtmlEscaped().replace(lit("\x01"), durationHtml) +
                 // i18n-ignore: Structural transcript HTML contains no user-visible prose.
                 lit("</a></td></tr></table><hr/>");

  if(message.activityExpanded)
  {
    // i18n-ignore: Structural transcript HTML contains no user-visible prose.
    html += lit("<div class=\"activity-list\">");
    const int firstActivity = qMax(0, message.activity.size() - maxActivities);
    if(firstActivity > 0)
      html += lit("<p><a href=\"replaylab-activity://more/") + token + lit("\">") + // i18n-ignore: HTML and internal activity pagination URI.
          QCoreApplication::translate("AIConversationView", "Show earlier activities (%1 remaining)").arg(firstActivity).toHtmlEscaped() + lit("</a></p>"); // i18n-ignore: HTML closing link/paragraph markup.
    for(int activityIndex = firstActivity; activityIndex < message.activity.size(); ++activityIndex)
    {
      const AIActivityItem &activity = message.activity[activityIndex];
      QString title = activity.title.trimmed();
      if(title.isEmpty())
        title = activityKindLabel(activity.kind);

      qint64 itemFinishedMs = activity.finishedTimestampMs;
      const bool itemRunning = itemFinishedMs <= 0 && !activity.completed && !activity.error;
      if(itemRunning)
        itemFinishedMs = nowMs;
      QString itemDuration;
      if(activity.timestampMs > 0 && itemFinishedMs >= activity.timestampMs)
        itemDuration = elapsedText(itemFinishedMs - activity.timestampMs);
      QString itemDurationHtml = itemDuration.toHtmlEscaped();
      if(itemRunning && !itemDuration.isEmpty() && elapsedAnchors != NULL)
      {
        const QString anchorName =
            lit("elapsed-i-") + elapsedToken + lit("-") + QString::number(activityIndex);    // i18n-ignore: internal anchor id.
        elapsedAnchors->insert(anchorName, qMakePair(activity.timestampMs, itemDuration));
        // i18n-ignore: Structural transcript HTML contains no user-visible prose.
        itemDurationHtml = lit("<a name=\"") + anchorName + lit("\">") + itemDurationHtml + lit("</a>");
      }

      const QString statusClass = activityStatusClass(activity);
      const QString marker =
          // i18n-ignore: HTML entities are visual status markers, not user-visible prose.
          activity.error ? lit("&#10005;") : (activity.completed ? lit("&#10003;") : lit("&#9679;"));
      const bool failedTool = activity.error &&
          // i18n-ignore: Backend activity kind identifiers, not display labels.
          (activity.kind == lit("tool") || activity.kind == lit("mcpToolCall"));
      const QString markerHint = failedTool
          ? QCoreApplication::translate("AIConversationView",
                                        "This tool call failed. The Agent may continue and retry.")
          : activityState(activity);
      // i18n-ignore: Structural transcript HTML contains no user-visible prose.
      html += lit("<div class=\"activity-row\"><table width=\"100%\" border=\"0\" ") +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              lit("cellspacing=\"0\" cellpadding=\"0\"><tr><td width=\"22\" valign=\"top\">") +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              lit("<span class=\"activity-marker ") + statusClass + lit("\" title=\"") +
              markerHint.toHtmlEscaped() + lit("\">") + marker +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              lit("</span></td><td><div class=\"activity-title\">") + title.toHtmlEscaped() +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              lit("</div><div class=\"activity-meta\"><span class=\"activity-kind\">") +
              activityKindLabel(activity.kind).toHtmlEscaped() + QLatin1String("</span>") +
              // i18n-ignore: Structural transcript HTML and separator entity contain no prose.
              lit("<span class=\"activity-state\"> &middot; ") +
              activityState(activity).toHtmlEscaped();
      if(!itemDuration.isEmpty())
        // i18n-ignore: HTML entity is a visual separator, not user-visible prose.
        html += lit(" &middot; ") + itemDurationHtml;
      // i18n-ignore: Structural transcript HTML contains no user-visible prose.
      html += lit("</span></div>");
      if(activity.liveText)
      {
        html += renderLiveActivityDetail(message, activity, liveTextAnchors);
      }
      else if(!activity.detail.trimmed().isEmpty())
      {
        // Activity detail is already an explicit public event summary. This view never requests or
        // renders private model reasoning; reasoning summaries stay plain text, while bounded tool
        // arguments/results use a dedicated code surface.
        html += renderActivityDetail(activity.detail,
                                     isCodeActivity(activity.kind, activity.detail));
      }
      // i18n-ignore: Structural transcript HTML contains no user-visible prose.
      html += lit("</td></tr></table></div>");
    }
    // i18n-ignore: Structural transcript HTML contains no user-visible prose.
    html += lit("</div>");
  }
  // i18n-ignore: Structural transcript HTML contains no user-visible prose.
  html += lit("</div>");
  return html;
}

const qint64 ConversationImageMaxBytes = 8LL * 1024LL * 1024LL;
const int ConversationImageMaxDimension = 16384;
const qint64 ConversationImageMaxPixels = 64LL * 1024LL * 1024LL;

QString conversationImageMediaTypeEssence(const QString &mediaType)
{
  return mediaType.section(QLatin1Char(';'), 0, 0).trimmed().toLower();
}

QString conversationRasterMediaTypeForFormat(const QByteArray &format)
{
  const QByteArray normalized = format.toLower();
  static const QSet<QByteArray> supportedFormats = []() {
    QSet<QByteArray> result;
    for(const QByteArray &candidate : QImageReader::supportedImageFormats())
      result.insert(candidate.toLower());
    return result;
  }();
  if(!supportedFormats.contains(normalized))
    return QString();

  // A document/vector reader can load referenced resources. Reject these codecs before asking the
  // reader for dimensions so persisted untrusted content never enters that decoding path.
  if(normalized == "svg" || normalized == "svgz" || normalized == "pdf" ||
     normalized == "eps" || normalized == "ps")    // i18n-ignore: image codec identifiers
    return QString();
  if(normalized == "png")    // i18n-ignore: Qt image codec identifier
    return lit("image/png");    // i18n-ignore: MIME type
  if(normalized == "jpg" || normalized == "jpeg")    // i18n-ignore: Qt codec identifiers
    return lit("image/jpeg");    // i18n-ignore: MIME type
  if(normalized == "gif")    // i18n-ignore: Qt image codec identifier
    return lit("image/gif");    // i18n-ignore: MIME type
  if(normalized == "webp")    // i18n-ignore: Qt image codec identifier
    return lit("image/webp");    // i18n-ignore: MIME type
  if(normalized == "bmp")    // i18n-ignore: Qt image codec identifier
    return lit("image/bmp");    // i18n-ignore: MIME type
  if(normalized == "ico" || normalized == "cur")    // i18n-ignore: codec identifiers
    return lit("image/x-icon");    // i18n-ignore: MIME type
  if(normalized == "tif" || normalized == "tiff")    // i18n-ignore: codec identifiers
    return lit("image/tiff");    // i18n-ignore: MIME type
  if(normalized == "tga")    // i18n-ignore: Qt image codec identifier
    return lit("image/x-tga");    // i18n-ignore: MIME type
  if(normalized == "pbm")    // i18n-ignore: Qt image codec identifier
    return lit("image/x-portable-bitmap");    // i18n-ignore: MIME type
  if(normalized == "pgm")    // i18n-ignore: Qt image codec identifier
    return lit("image/x-portable-graymap");    // i18n-ignore: MIME type
  if(normalized == "ppm")    // i18n-ignore: Qt image codec identifier
    return lit("image/x-portable-pixmap");    // i18n-ignore: MIME type
  if(normalized == "xbm")    // i18n-ignore: Qt image codec identifier
    return lit("image/x-xbitmap");    // i18n-ignore: MIME type
  if(normalized == "xpm")    // i18n-ignore: Qt image codec identifier
    return lit("image/x-xpixmap");    // i18n-ignore: MIME type
  if(normalized == "jp2" || normalized == "j2k" ||
     normalized == "jpx")    // i18n-ignore: codec identifiers
    return lit("image/jp2");    // i18n-ignore: MIME type
  if(normalized == "mng")    // i18n-ignore: Qt image codec identifier
    return lit("image/x-mng");    // i18n-ignore: MIME type
  if(normalized == "wbmp")    // i18n-ignore: Qt image codec identifier
    return lit("image/vnd.wap.wbmp");    // i18n-ignore: MIME type
  if(normalized == "avif")    // i18n-ignore: Qt image codec identifier
    return lit("image/avif");    // i18n-ignore: MIME type
  if(normalized == "heic")    // i18n-ignore: Qt image codec identifier
    return lit("image/heic");    // i18n-ignore: MIME type
  if(normalized == "heif")    // i18n-ignore: Qt image codec identifier
    return lit("image/heif");    // i18n-ignore: MIME type

  // A dynamically installed Qt raster codec remains usable without a fixed source-code list.
  // i18n-ignore: Constructed MIME type is attachment metadata, not visible prose.
  return lit("image/x-") + QString::fromLatin1(normalized);
}

bool conversationDecodedRasterImageIsSafe(const QImage &image)
{
  return !image.isNull() && image.width() > 0 && image.height() > 0 &&
         image.width() <= ConversationImageMaxDimension &&
         image.height() <= ConversationImageMaxDimension &&
         qint64(image.width()) * qint64(image.height()) <= ConversationImageMaxPixels;
}

bool conversationImageBytes(const AIChatAttachment &attachment, QByteArray *bytes)
{
  if(bytes)
    bytes->clear();

  // Only immutable payloads already embedded in the conversation are eligible. A persisted path
  // reference is metadata, not permission to reopen a historical local file while rendering.
  if(attachment.kind != QLatin1String("image") && attachment.kind != QLatin1String("file"))
    return false;
  if(attachment.encoding != QLatin1String("base64") || attachment.content.isEmpty() ||
     attachment.content.size() > ((ConversationImageMaxBytes + 2) / 3) * 4)
    return false;
  const QByteArray encoded = attachment.content.toLatin1();
  if(QString::fromLatin1(encoded) != attachment.content)
    return false;
  const QByteArray decoded = QByteArray::fromBase64(encoded);
  if(decoded.isEmpty() || decoded.size() > ConversationImageMaxBytes ||
     decoded.toBase64() != encoded)
    return false;
  if(bytes)
    *bytes = decoded;
  return true;
}

bool conversationAttachmentIsImage(const AIChatAttachment &attachment)
{
  if(attachment.kind == QLatin1String("image"))
    return true;
  return attachment.kind == QLatin1String("file") &&
         conversationImageMediaTypeEssence(attachment.mediaType).startsWith(
             QLatin1String("image/"));
}

QImage conversationImageThumbnail(const AIChatAttachment &attachment, const QSize &bounds)
{
  const QString mediaType = conversationImageMediaTypeEssence(attachment.mediaType);
  if(!bounds.isValid() || bounds.width() <= 0 || bounds.height() <= 0 ||
     !conversationAttachmentIsImage(attachment) ||
     !mediaType.startsWith(QLatin1String("image/")))
    return QImage();

  QByteArray bytes;
  if(!conversationImageBytes(attachment, &bytes))
    return QImage();
  QBuffer buffer;
  buffer.setData(bytes);
  if(!buffer.open(QIODevice::ReadOnly))
    return QImage();
  QImageReader reader(&buffer);
  reader.setDecideFormatFromContent(true);
  reader.setAutoTransform(true);
  const QString detectedMediaType = conversationRasterMediaTypeForFormat(reader.format());
  if(!detectedMediaType.isEmpty() && detectedMediaType == mediaType && reader.canRead())
  {
    const QSize size = reader.size();
    if(size.isValid() && size.width() > 0 && size.height() > 0 &&
       size.width() <= ConversationImageMaxDimension &&
       size.height() <= ConversationImageMaxDimension &&
       qint64(size.width()) * qint64(size.height()) <= ConversationImageMaxPixels)
    {
      const QSize thumbnailSize = size.scaled(bounds, Qt::KeepAspectRatio);
      if(thumbnailSize.width() > 0 && thumbnailSize.height() > 0)
      {
        reader.setScaledSize(thumbnailSize);
        const QImage image = reader.read();
        if(conversationDecodedRasterImageIsSafe(image))
          return image;
      }
    }
  }

  // Non-provider-native raster attachments can use an installed Windows imaging codec. Only the
  // immutable embedded bytes are decoded; persisted file-ref paths are never reopened here.
  if(attachment.kind == QLatin1String("file"))
  {
    const QImage image = AIFileThumbnail::decodeRasterData(
                             bytes, bounds, ConversationImageMaxBytes)
                             .image;
    if(conversationDecodedRasterImageIsSafe(image))
      return image;
  }
  return QImage();
}

QString artifactBadgeText(const QJsonObject &stats)
{
  QStringList parts;
  if(stats.contains(lit("addedChars")) || stats.contains(lit("removedChars")))    // i18n-ignore: transcript protocol identifier, not UI prose.
  {
    const qint64 added = qint64(stats.value(lit("addedChars")).toDouble());    // i18n-ignore: transcript protocol identifier, not UI prose.
    const qint64 removed = qint64(stats.value(lit("removedChars")).toDouble());
    // i18n-ignore: Numeric change badge; signs are presentation, not prose.
    parts << lit("+%1").arg(QLocale().toString(added)) << lit("-%1").arg(QLocale().toString(removed));
  }
  else if(stats.contains(lit("chars")))    // i18n-ignore: transcript protocol identifier, not UI prose.
    parts << QCoreApplication::translate("AIConversationView", "%1 chars")
                 .arg(QLocale().toString(qint64(stats.value(lit("chars")).toDouble())));    // i18n-ignore: transcript protocol identifier, not UI prose.
  else if(stats.contains(lit("files")))
    parts << QCoreApplication::translate("AIConversationView", "%1 files")
                 .arg(QLocale().toString(qint64(stats.value(lit("files")).toDouble())));    // i18n-ignore: transcript protocol identifier, not UI prose.
  else if(stats.contains(lit("bytes")))
    parts << QCoreApplication::translate("AIConversationView", "%1 bytes")
                 .arg(QLocale().toString(qint64(stats.value(lit("bytes")).toDouble())));    // i18n-ignore: transcript protocol identifier, not UI prose.
  // i18n-ignore: Separator glyph is presentation, not prose.
  return parts.join(lit(" "));
}

QString artifactFileName(const QString &path)
{
  const int slash = qMax(path.lastIndexOf(QLatin1Char('/')), path.lastIndexOf(QLatin1Char('\\')));
  return (slash >= 0 && slash + 1 < path.size()) ? path.mid(slash + 1) : path;
}

QString artifactRowHtml(const QString &path, const QString &label, const QString &badge,
                        const QString &badgeClass, const QString &tooltip)
{
  // i18n-ignore: Internal link scheme routed to the transcript link handler.
  const QString href = lit("replaylab-artifact://open/") + QString::fromUtf8(QUrl::toPercentEncoding(path));
  // i18n-ignore: Structural transcript HTML contains no user-visible prose.
  QString row = lit("<tr><td class=\"artifact-row\"><a class=\"artifact-link\" href=\"") +
                href.toHtmlEscaped() + lit("\" title=\"") + tooltip.toHtmlEscaped() + lit("\">") +    // i18n-ignore: transcript protocol identifier, not UI prose.
                label.toHtmlEscaped() + lit("</a></td><td class=\"artifact-badge ") + badgeClass +
                lit("\" align=\"right\">") + badge.toHtmlEscaped() + lit("</td></tr>");    // i18n-ignore: transcript protocol identifier, not UI prose.
  return row;
}

// One card per assistant message: what the run produced, and what changed inside it.
// A directory artifact (a resource export) lists its own files, so the receipt is
// readable without opening the folder.
const int ArtifactRowsCollapsed = 3;

QString renderMessageArtifacts(const AIChatMessage &message, bool expanded, int messageIndex)
{
  if(message.artifacts.isEmpty())
    return QString();

  qint64 totalAdded = 0, totalRemoved = 0, totalFiles = 0, totalBytes = 0;
  bool hasChangeCounts = false;
  QStringList rowList;
  QString rows;
  for(int index = 0; index < message.artifacts.size(); ++index)
  {
    const QJsonObject artifact = message.artifacts.at(index).toObject();
    const QString path = artifact.value(lit("path")).toString();    // i18n-ignore: transcript protocol identifier, not UI prose.
    if(path.isEmpty())
      continue;
    const QJsonObject stats = artifact.value(lit("stats")).toObject();    // i18n-ignore: transcript protocol identifier, not UI prose.
    const QString status = artifact.value(lit("status")).toString();
    QString tooltip = path;
    const QString note = artifact.value(lit("note")).toString().trimmed();    // i18n-ignore: transcript protocol identifier, not UI prose.
    if(!note.isEmpty())
      // i18n-ignore: Newline separates path from note in a tooltip.
      tooltip += lit("\n") + note;

    QString badge = artifactBadgeText(stats);
    // i18n-ignore: Artifact status values are backend protocol identifiers.
    if(status == lit("failed"))
      badge = QCoreApplication::translate("AIConversationView", "failed");
    // i18n-ignore: Artifact status values are backend protocol identifiers.
    else if(status == lit("partial"))
      badge = badge.isEmpty()
                  ? QCoreApplication::translate("AIConversationView", "partial")
                  : badge + lit(" ") + QCoreApplication::translate("AIConversationView", "partial");

    if(stats.contains(lit("addedChars")) || stats.contains(lit("removedChars")))    // i18n-ignore: transcript protocol identifier, not UI prose.
    {
      hasChangeCounts = true;
      totalAdded += qint64(stats.value(lit("addedChars")).toDouble());    // i18n-ignore: transcript protocol identifier, not UI prose.
      totalRemoved += qint64(stats.value(lit("removedChars")).toDouble());
    }
    if(stats.value(lit("kind")).toString() == lit("directory"))    // i18n-ignore: transcript protocol identifier, not UI prose.
      totalFiles += qint64(stats.value(lit("files")).toDouble());
    else
      totalFiles += 1;
    totalBytes += qint64(stats.value(lit("bytes")).toDouble());    // i18n-ignore: transcript protocol identifier, not UI prose.

    rowList << artifactRowHtml(path, artifactFileName(path), badge,
                               // i18n-ignore: CSS class selection from a protocol status value.
                               status == lit("failed") ? lit("artifact-failed") : QString(), tooltip);

    const QJsonArray children = stats.value(lit("children")).toArray();    // i18n-ignore: transcript protocol identifier, not UI prose.
    for(const QJsonValue &value : children)
    {
      const QJsonObject child = value.toObject();
      const QString childPath = child.value(lit("path")).toString();    // i18n-ignore: transcript protocol identifier, not UI prose.
      const QString childName = child.value(lit("name")).toString();
      if(childPath.isEmpty() || childName.isEmpty())
        continue;
      const QString childBadge = QCoreApplication::translate("AIConversationView", "%1 bytes")
                                     .arg(QLocale().toString(qint64(child.value(lit("bytes")).toDouble())));    // i18n-ignore: transcript protocol identifier, not UI prose.
      // i18n-ignore: Structural indent for a nested file row.
      rowList << artifactRowHtml(childPath, lit("    ") + childName, childBadge,
                                 lit("artifact-child"), childPath);    // i18n-ignore: transcript protocol identifier, not UI prose.
    }
    if(stats.value(lit("childrenTruncated")).toBool())    // i18n-ignore: transcript protocol identifier, not UI prose.
      // i18n-ignore: Structural transcript HTML; the visible text is translated.
      rowList << lit("<tr><td class=\"artifact-row artifact-child\" colspan=\"2\">") +
                     QCoreApplication::translate("AIConversationView", "... and more files in this folder")
                         .toHtmlEscaped() +
                     lit("</td></tr>");    // i18n-ignore: transcript protocol identifier, not UI prose.
  }
  if(rowList.isEmpty())
    return QString();
  // A few rows stay visible; the rest fold behind one line, so a long receipt
  // never pushes the reply off the screen.
  const int hidden = expanded ? 0 : qMax(0, rowList.size() - ArtifactRowsCollapsed);
  for(int index = 0; index < rowList.size() - hidden; ++index)
    rows += rowList.at(index);
  if(rowList.size() > ArtifactRowsCollapsed)
  {
    const QString token = QString::fromLatin1(QUrl::toPercentEncoding(message.id));
    // i18n-ignore: Internal link scheme routed to the transcript link handler.
    const QString href = lit("replaylab-artifacts://toggle/") + token + lit("?expanded=") +
                         (expanded ? lit("0") : lit("1"));
    const QString label =
        expanded ? QCoreApplication::translate("AIConversationView", "Show fewer")
                 : QCoreApplication::translate("AIConversationView", "Show %1 more files")
                       .arg(QLocale().toString(hidden));
    // i18n-ignore: Structural transcript HTML; the visible text is translated.
    rows += lit("<tr><td class=\"artifact-row artifact-more\" colspan=\"2\"><a class=\"artifact-toggle\" href=\"") +
            href.toHtmlEscaped() + lit("\">") + label.toHtmlEscaped() +
            // i18n-ignore: HTML entities are visual disclosure indicators, not prose.
            (expanded ? lit(" &#9652;") : lit(" &#9662;")) + lit("</a></td></tr>");
  }

  QString heading = hasChangeCounts
                        ? QCoreApplication::translate("AIConversationView", "Edited %1 files")
                              .arg(QLocale().toString(totalFiles))
                        : QCoreApplication::translate("AIConversationView", "Produced %1 files")
                              .arg(QLocale().toString(totalFiles));
  QString summary;
  if(hasChangeCounts)
    // i18n-ignore: Numeric change badge; signs are presentation, not prose.
    summary = lit("<span class=\"artifact-add\">+%1</span> <span class=\"artifact-del\">-%2</span>")
                  .arg(QLocale().toString(totalAdded), QLocale().toString(totalRemoved));
  else if(totalBytes > 0)
    summary = QCoreApplication::translate("AIConversationView", "%1 bytes")
                  .arg(QLocale().toString(totalBytes))
                  .toHtmlEscaped();

  // The card's rounded dark panel is painted by the browser between the two
  // named anchors; the table itself carries no background.
  // i18n-ignore: Generated anchor prefix is an internal transcript identifier.
  const QString cardStart = lit("replaylab-card-start-") + QString::number(messageIndex);
  // i18n-ignore: Generated anchor prefix is an internal transcript identifier.
  const QString cardEnd = lit("replaylab-card-end-") + QString::number(messageIndex);
  // i18n-ignore: Structural transcript HTML contains no user-visible prose.
  return lit("<table class=\"artifact-card\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\">") +
         lit("<tr><td class=\"artifact-head\"><a name=\"") + cardStart + lit("\">&#8203;</a>") +    // i18n-ignore: transcript protocol identifier, not UI prose.
         heading.toHtmlEscaped() +
         lit("</td><td class=\"artifact-head\" align=\"right\">") + summary + lit("</td></tr>") + // i18n-ignore: structural artifact table HTML.
         rows +
         // i18n-ignore: Structural transcript HTML and zero-width marker contain no prose.
         lit("<tr><td class=\"artifact-foot\" colspan=\"2\"><a name=\"") + cardEnd +
         lit("\">&#8203;</a></td></tr></table>");    // i18n-ignore: transcript protocol identifier, not UI prose.
}

QString renderMessageAttachments(const QVector<AIChatAttachment> &attachments,
                                 QTextDocument *document)
{
  if(attachments.isEmpty())
    return QString();

  // i18n-ignore: Structural transcript HTML and the paperclip entity contain no prose.
  QString html = lit("<div class=\"message-attachments\">");
  for(const AIChatAttachment &attachment : attachments)
  {
    if(AIHideAnnotationOriginal(attachment, attachments))
      continue;
    const bool editableMarks = !attachment.annotationPairId().isEmpty();
    if(editableMarks)
    {
      // i18n-ignore: internal action URL and structural transcript HTML.
      html += lit("<a href=\"replaylab-annotation://edit/") +
              QString::fromLatin1(QUrl::toPercentEncoding(attachment.id)) + lit("\">");
    }
    QString title = attachment.title.simplified().left(160);
    if(title.isEmpty())
      title = QCoreApplication::translate("AIConversationView", "Attachment");
    QString label;
    QString icon;
    bool renderedThumbnail = false;
    if(conversationAttachmentIsImage(attachment))
    {
      label = QCoreApplication::translate("AIConversationView", "Image: %1").arg(title);
      // i18n-ignore: Structural transcript HTML and the framed-picture entity contain no prose.
      icon = lit("&#128444;&nbsp;");

      const QImage image = conversationImageThumbnail(attachment, QSize(160, 120));
      if(document && !image.isNull())
      {
        // i18n-ignore: Attachment resource URL is an internal document key, not visible prose.
        const QUrl resourceUrl(lit("replaylab-attachment://") +
                               QString::fromLatin1(QUrl::toPercentEncoding(attachment.id)));
        document->addResource(QTextDocument::ImageResource, resourceUrl, image);
        const QString escapedLabel = label.toHtmlEscaped();
        // i18n-ignore: Structural transcript HTML contains no user-visible prose.
        html += lit("<img src=\"") + resourceUrl.toString(QUrl::FullyEncoded).toHtmlEscaped() +
                // i18n-ignore: Structural transcript HTML dimensions contain no prose.
                lit("\" width=\"") + QString::number(image.width()) +
                // i18n-ignore: structural transcript HTML dimensions contain no prose.
                lit("\" height=\"") + QString::number(image.height()) +
                // i18n-ignore: structural transcript HTML attribute contains no prose.
                lit("\" title=\"") + escapedLabel + lit("\" alt=\"") +
                escapedLabel +    // i18n-ignore: structural transcript HTML
                lit("\"/> ");     // i18n-ignore: structural transcript HTML
        renderedThumbnail = true;
      }
    }
    else if(attachment.kind == QLatin1String("file") ||
            attachment.kind == QLatin1String("file-ref"))
    {
      label = QCoreApplication::translate("AIConversationView", "File: %1").arg(title);
      // i18n-ignore: Structural transcript HTML and the paperclip entity contain no prose.
      icon = lit("&#128206;&nbsp;");
    }
    else
    {
      label = QCoreApplication::translate("AIConversationView", "Text: %1").arg(title);
      // i18n-ignore: Structural transcript HTML and the page entity contain no prose.
      icon = lit("&#128196;&nbsp;");
    }
    if(!renderedThumbnail)
      // i18n-ignore: Structural transcript HTML contains no prose.
      html += lit("<span class=\"message-attachment\">") + icon + label.toHtmlEscaped() +
              lit("</span>");    // i18n-ignore: structural transcript HTML
    if(editableMarks)
      html += QLatin1String("</a>");
  }
  // i18n-ignore: Structural transcript HTML contains no user-visible prose.
  return html + lit("</div>");
}
}    // namespace

// QTextDocument's Qt 5.9 rich-text subset does not clip table-cell backgrounds to CSS corner
// radii. Paint the user-message surface explicitly so the bubble remains rounded on every
// supported Qt backend while retaining native text selection and link handling.
class AIRoundedTextBrowser : public QTextBrowser
{
public:
  struct ElapsedOverlay
  {
    QTextCursor cursor;
    int length = 0;
    QTextCharFormat format;
    QString text;
  };

  explicit AIRoundedTextBrowser(QWidget *parent = NULL) : QTextBrowser(parent)
  {
    // The background is painted here (base colour, then the opaque rounded cards)
    // before the document draws its text, so a card can be a real dark panel with
    // light text rather than a tint laid over it.
    viewport()->setAutoFillBackground(false);
  }

  void setCardAnchors(const QVector<QPair<QString, QString>> &anchors)
  {
    m_CardCursors = resolveAnchorPairs(anchors);
    viewport()->update();
  }

  void setBubbleAnchors(const QVector<QPair<QString, QString>> &anchors)
  {
    m_BubbleCursors = resolveAnchorPairs(anchors);
    viewport()->update();
  }

  void setElapsedOverlays(const QVector<ElapsedOverlay> &overlays)
  {
    m_ElapsedOverlays = overlays;
    viewport()->update();
  }

private:
  QVector<QPair<QTextCursor, QTextCursor>> resolveAnchorPairs(
      const QVector<QPair<QString, QString>> &anchors)
  {
    QVector<QPair<QTextCursor, QTextCursor>> resolved;
    if(anchors.isEmpty() || !document())
      return resolved;

    QHash<QString, QTextCursor> anchorCursors;
    for(QTextBlock block = document()->begin(); block.isValid(); block = block.next())
    {
      for(QTextBlock::iterator it = block.begin(); !it.atEnd(); ++it)
      {
        const QTextFragment fragment = it.fragment();
        if(!fragment.isValid())
          continue;
        const QStringList names = fragment.charFormat().anchorNames();
        if(names.isEmpty())
          continue;
        QTextCursor cursor(document());
        cursor.setPosition(fragment.position());
        for(const QString &name : names)
          anchorCursors[name] = cursor;
      }
    }
    for(const QPair<QString, QString> &names : anchors)
    {
      if(anchorCursors.contains(names.first) && anchorCursors.contains(names.second))
        resolved.push_back(
            qMakePair(anchorCursors.value(names.first), anchorCursors.value(names.second)));
    }
    return resolved;
  }

  QRectF spanRect(const QPair<QTextCursor, QTextCursor> &cursors, qreal leftInset,
                  qreal topInset, qreal rightInset, qreal bottomInset) const
  {
    const QRect start = cursorRect(cursors.first);
    const QRect end = cursorRect(cursors.second);
    if(end.bottom() < start.top())
      return QRectF();
    return QRectF(start.left() - leftInset, start.top() - topInset,
                  viewport()->width() - start.left() - rightInset,
                  end.bottom() - start.top() + topInset + bottomInset);
  }

protected:
  void paintEvent(QPaintEvent *event) override
  {
    {
      QPainter background(viewport());
      background.fillRect(event->rect(), palette().brush(QPalette::Base));
      if(!m_CardCursors.isEmpty() && document())
      {
        background.setRenderHint(QPainter::Antialiasing, true);
        background.setClipRect(event->rect());
        background.setBrush(QColor(0x18, 0x18, 0x18));
        background.setPen(QPen(QColor(0x3c, 0x3c, 0x3c), 1.0));
        for(const QPair<QTextCursor, QTextCursor> &cursors : m_CardCursors)
        {
          const QRectF card = spanRect(cursors, 8.0, 6.0, 9.0, 8.0);
          if(card.width() <= 4.0 || card.height() <= 4.0 || !card.intersects(QRectF(event->rect())))
            continue;
          background.drawRoundedRect(card, 10.0, 10.0);
        }
      }
    }
    QTextBrowser::paintEvent(event);
    // Keep clocks out of QTextDocument's incremental rich-text layout. Qt 5.9
    // can invalidate a QTextLine when a duration is edited amid streamed tables.
    // The original span still owns selection, links and document geometry.
    if(!m_ElapsedOverlays.isEmpty())
    {
      QPainter clock(viewport());
      clock.setClipRect(event->rect());
      const QTextCursor selection = textCursor();
      for(const ElapsedOverlay &overlay : m_ElapsedOverlays)
      {
        if(overlay.cursor.isNull() || overlay.cursor.document() != document())
          continue;
        const int position = overlay.cursor.position();
        if(selection.hasSelection() && selection.selectionStart() < position + overlay.length &&
           selection.selectionEnd() > position)
          continue;
        QTextCursor end = overlay.cursor;
        end.setPosition(position + overlay.length);
        const QRect first = cursorRect(overlay.cursor), last = cursorRect(end);
        if(first.top() != last.top())
          continue;
        const QFont labelFont = overlay.format.font().resolve(font());
        clock.setFont(labelFont);
        const qreal width =
            qMax<qreal>(last.left() - first.left(), QFontMetricsF(labelFont).width(overlay.text)) + 2;
        const QRectF area(first.left(), first.top(),
                          qMin<qreal>(width, viewport()->width() - first.left()), first.height());
        if(area.width() <= 0 || !area.intersects(QRectF(event->rect())))
          continue;
        clock.fillRect(area, palette().brush(QPalette::Base));
        clock.setPen(overlay.format.foreground().style() == Qt::NoBrush
                         ? palette().color(QPalette::Text)
                         : overlay.format.foreground().color());
        clock.drawText(area, Qt::AlignLeft | Qt::AlignVCenter | Qt::TextSingleLine, overlay.text);
      }
    }
    if(m_BubbleCursors.isEmpty() || !document())
      return;

    QPainter painter(viewport());
    painter.setRenderHint(QPainter::Antialiasing, true);
    QColor fill = palette().color(QPalette::AlternateBase);
    fill.setAlpha(46);
    QColor outline = palette().color(QPalette::Mid);
    outline.setAlpha(190);
    painter.setBrush(fill);
    painter.setPen(QPen(outline, 1.0));
    painter.setClipRect(event->rect());
    for(const QPair<QTextCursor, QTextCursor> &cursors : m_BubbleCursors)
    {
      const QRect start = cursorRect(cursors.first);
      const QRect end = cursorRect(cursors.second);
      if(end.bottom() < start.top())
        continue;
      QRectF bubble(start.left() - 8.0, start.top() - 5.0, viewport()->width() - start.left() - 9.0,
                    end.bottom() - start.top() + 10.0);
      if(bubble.width() <= 4.0 || bubble.height() <= 4.0 || !bubble.intersects(QRectF(event->rect())))
        continue;
      const qreal radius = qMin<qreal>(18.0, bubble.height() / 2.0);
      painter.drawRoundedRect(bubble, radius, radius);
    }
  }

private:
  QVector<QPair<QTextCursor, QTextCursor>> m_BubbleCursors;
  QVector<QPair<QTextCursor, QTextCursor>> m_CardCursors;
  QVector<ElapsedOverlay> m_ElapsedOverlays;
};

static QColor blendedColor(const QColor &from, const QColor &to, int toPercent)
{
  const int boundedPercent = qBound(0, toPercent, 100);
  const int fromPercent = 100 - boundedPercent;
  return QColor((from.red() * fromPercent + to.red() * boundedPercent) / 100,
                (from.green() * fromPercent + to.green() * boundedPercent) / 100,
                (from.blue() * fromPercent + to.blue() * boundedPercent) / 100,
                (from.alpha() * fromPercent + to.alpha() * boundedPercent) / 100);
}

static QString boundedTurnPreview(const QString &text, int maxCharacters)
{
  QString preview = text.simplified();
  if(preview.size() <= maxCharacters)
    return preview;

  preview = preview.left(qMax(0, maxCharacters - 1)).trimmed();
  if(!preview.isEmpty() && preview.at(preview.size() - 1).isHighSurrogate())
    preview.chop(1);
  return preview + QChar(0x2026);
}

class AITurnPreviewPopup : public QWidget
{
public:
  explicit AITurnPreviewPopup(QWidget *parent = NULL)
      : QWidget(parent, Qt::ToolTip | Qt::FramelessWindowHint)
  {
    setAttribute(Qt::WA_ShowWithoutActivating);
    setAttribute(Qt::WA_TransparentForMouseEvents);
    setAttribute(Qt::WA_TranslucentBackground);
    setFocusPolicy(Qt::NoFocus);

    QVBoxLayout *layout = new QVBoxLayout(this);
    layout->setContentsMargins(12, 9, 12, 10);
    layout->setSpacing(5);

    m_UserPreview = new QLabel(this);
    m_UserPreview->setTextFormat(Qt::PlainText);
    m_UserPreview->setWordWrap(true);
    m_UserPreview->setAlignment(Qt::AlignLeft | Qt::AlignTop);
    layout->addWidget(m_UserPreview);

    m_AssistantPreview = new QLabel(this);
    m_AssistantPreview->setTextFormat(Qt::PlainText);
    m_AssistantPreview->setWordWrap(true);
    m_AssistantPreview->setAlignment(Qt::AlignLeft | Qt::AlignTop);
    layout->addWidget(m_AssistantPreview);

    updatePresentation();
    hide();
  }

  ~AITurnPreviewPopup() override
  {
    if(m_ApplicationEventFilterInstalled && qApp)
      qApp->removeEventFilter(this);
  }

  void setPreview(const QString &userPreview, const QString &assistantPreview, int contentWidth,
                  int maximumPopupWidth, int maximumPopupHeight)
  {
    contentWidth = qMax(1, contentWidth);
    maximumPopupHeight = qMax(1, maximumPopupHeight);
    m_UserPreview->setFixedWidth(contentWidth);
    m_AssistantPreview->setFixedWidth(contentWidth);

    const int verticalChrome = 24;
    const int availableTextHeight = qMax(1, maximumPopupHeight - verticalChrome);
    const int userLineHeight = qMax(1, QFontMetrics(m_UserPreview->font()).lineSpacing());
    const int assistantLineHeight =
        qMax(1, QFontMetrics(m_AssistantPreview->font()).lineSpacing());
    int userHeight = availableTextHeight;
    int assistantHeight = 0;
    if(!assistantPreview.isEmpty())
    {
      userHeight = qMin(userLineHeight * 2, qMax(userLineHeight, availableTextHeight / 3));
      assistantHeight = qMax(assistantLineHeight, availableTextHeight - userHeight - 5);
    }

    setLabelPreview(m_UserPreview, userPreview, userHeight);
    setLabelPreview(m_AssistantPreview, assistantPreview, assistantHeight);
    m_AssistantPreview->setVisible(!assistantPreview.isEmpty());
    setMaximumSize(qMax(1, qMin(contentWidth + 24, maximumPopupWidth)), maximumPopupHeight);
    layout()->activate();
    adjustSize();
  }

protected:
  bool eventFilter(QObject *watched, QEvent *event) override
  {
    if(isVisible() && event)
    {
      const bool ownerWindowDeactivated =
          event->type() == QEvent::WindowDeactivate && parentWidget() &&
          watched == parentWidget()->window();
      if(event->type() == QEvent::ApplicationDeactivate ||
         event->type() == QEvent::MouseButtonPress || event->type() == QEvent::Wheel ||
         ownerWindowDeactivated)
        hide();
    }
    return QWidget::eventFilter(watched, event);
  }

  void hideEvent(QHideEvent *event) override
  {
    if(m_ApplicationEventFilterInstalled && qApp)
    {
      qApp->removeEventFilter(this);
      m_ApplicationEventFilterInstalled = false;
    }
    QWidget::hideEvent(event);
  }

  void paintEvent(QPaintEvent *) override
  {
    QPainter painter(this);
    painter.setRenderHint(QPainter::Antialiasing, true);
    const QColor fill = palette().color(QPalette::ToolTipBase);
    const QColor outline =
        blendedColor(fill, palette().color(QPalette::ToolTipText), 24);
    painter.setPen(QPen(outline, 1.0));
    painter.setBrush(fill);
    painter.drawRoundedRect(QRectF(rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10.0, 10.0);
  }

  void changeEvent(QEvent *event) override
  {
    QWidget::changeEvent(event);
    if(event && (event->type() == QEvent::PaletteChange || event->type() == QEvent::FontChange ||
                 event->type() == QEvent::StyleChange))
      updatePresentation();
  }

  void showEvent(QShowEvent *event) override
  {
    QWidget::showEvent(event);
    if(!m_ApplicationEventFilterInstalled && qApp)
    {
      qApp->installEventFilter(this);
      m_ApplicationEventFilterInstalled = true;
    }
  }

private:
  static void setLabelPreview(QLabel *label, const QString &text, int maximumHeight)
  {
    if(!label)
      return;

    label->setMaximumHeight(qMax(1, maximumHeight));
    label->setText(text);
    if(text.isEmpty() || label->heightForWidth(label->width()) <= maximumHeight)
      return;

    int low = 0;
    int high = text.size();
    while(low < high)
    {
      const int candidateLength = (low + high + 1) / 2;
      QString candidate = text.left(candidateLength).trimmed();
      if(!candidate.isEmpty() && candidate.at(candidate.size() - 1).isHighSurrogate())
        candidate.chop(1);
      candidate += QChar(0x2026);
      label->setText(candidate);
      if(label->heightForWidth(label->width()) <= maximumHeight)
        low = candidateLength;
      else
        high = candidateLength - 1;
    }

    QString fitted = text.left(low).trimmed();
    if(!fitted.isEmpty() && fitted.at(fitted.size() - 1).isHighSurrogate())
      fitted.chop(1);
    label->setText(fitted + QChar(0x2026));
  }

  void updatePresentation()
  {
    if(!m_UserPreview || !m_AssistantPreview)
      return;

    QFont userFont = font();
    userFont.setWeight(QFont::DemiBold);
    m_UserPreview->setFont(userFont);

    QPalette userPalette = palette();
    userPalette.setColor(QPalette::WindowText, palette().color(QPalette::ToolTipText));
    m_UserPreview->setPalette(userPalette);

    QPalette assistantPalette = palette();
    assistantPalette.setColor(
        QPalette::WindowText,
        blendedColor(palette().color(QPalette::ToolTipText),
                     palette().color(QPalette::ToolTipBase), 28));
    m_AssistantPreview->setPalette(assistantPalette);
  }

  QLabel *m_UserPreview = NULL;
  QLabel *m_AssistantPreview = NULL;
  bool m_ApplicationEventFilterInstalled = false;
};

class AITurnRailWidget : public QWidget
{
public:
  struct Turn
  {
    QString messageId;
    QString userPreview;
    QString assistantPreview;
  };

  explicit AITurnRailWidget(QWidget *parent = NULL) : QWidget(parent)
  {
    setFixedWidth(36);
    setMouseTracking(true);
    setFocusPolicy(Qt::StrongFocus);
    m_PreviewPopup = new AITurnPreviewPopup(this);
  }

  void setTurns(const QVector<Turn> &turns)
  {
    const bool wasHovering = m_Hovered >= 0;
    QString focusedMessageId;
    if(m_Focused >= 0 && m_Focused < m_Turns.size())
      focusedMessageId = m_Turns[m_Focused].messageId;
    m_Turns = turns;
    m_Hovered = -1;
    m_Focused = -1;
    if(!focusedMessageId.isEmpty())
    {
      for(int i = 0; i < m_Turns.size(); ++i)
      {
        if(m_Turns[i].messageId == focusedMessageId)
        {
          m_Focused = i;
          break;
        }
      }
    }
    if(m_Focused < 0 && hasFocus() && !m_Turns.isEmpty())
      m_Focused = 0;
    if(wasHovering && isVisible() && isActiveWindow() &&
       QApplication::applicationState() == Qt::ApplicationActive)
    {
      const QPoint pointer = mapFromGlobal(QCursor::pos());
      if(rect().contains(pointer))
        m_Hovered = turnAt(pointer.y());
    }

    if(m_Hovered >= 0)
    {
      setCursor(Qt::PointingHandCursor);
      showPreview(m_Hovered);
    }
    else
    {
      unsetCursor();
      m_PreviewPopup->hide();
    }
    updateAccessiblePreview(hasFocus() ? m_Focused : m_Hovered);
    update();
  }

  void setActivateHandler(std::function<void(const QString &)> handler)
  {
    m_ActivateHandler = std::move(handler);
  }

protected:
  void focusInEvent(QFocusEvent *event) override
  {
    if(m_Focused < 0 && !m_Turns.isEmpty())
      m_Focused = m_Hovered >= 0 ? m_Hovered : 0;
    updateAccessiblePreview(m_Focused);
    if(m_Focused >= 0)
      showPreview(m_Focused);
    update();
    QWidget::focusInEvent(event);
  }

  void focusOutEvent(QFocusEvent *event) override
  {
    m_PreviewPopup->hide();
    update();
    QWidget::focusOutEvent(event);
  }

  void hideEvent(QHideEvent *event) override
  {
    m_PreviewPopup->hide();
    QWidget::hideEvent(event);
  }

  void keyPressEvent(QKeyEvent *event) override
  {
    if(m_Turns.isEmpty())
    {
      QWidget::keyPressEvent(event);
      return;
    }

    int target = qBound(0, m_Focused < 0 ? 0 : m_Focused, m_Turns.size() - 1);
    switch(event->key())
    {
      case Qt::Key_Up:
      case Qt::Key_Left: target = qMax(0, target - 1); break;
      case Qt::Key_Down:
      case Qt::Key_Right: target = qMin(m_Turns.size() - 1, target + 1); break;
      case Qt::Key_Home: target = 0; break;
      case Qt::Key_End: target = m_Turns.size() - 1; break;
      case Qt::Key_Return:
      case Qt::Key_Enter:
      case Qt::Key_Space:
        if(m_Focused >= 0 && m_Focused < m_Turns.size() && m_ActivateHandler)
          m_ActivateHandler(m_Turns[m_Focused].messageId);
        m_PreviewPopup->hide();
        event->accept();
        return;
      case Qt::Key_Escape:
        m_PreviewPopup->hide();
        event->accept();
        return;
      default: QWidget::keyPressEvent(event); return;
    }

    m_Focused = target;
    m_Hovered = -1;
    unsetCursor();
    updateAccessiblePreview(m_Focused);
    showPreview(m_Focused);
    update();
    event->accept();
  }

  void paintEvent(QPaintEvent *) override
  {
    QPainter painter(this);
    painter.setRenderHint(QPainter::Antialiasing, true);
    const QColor normal = blendedColor(palette().color(QPalette::Midlight),
                                       palette().color(QPalette::Text), 28);
    const QColor active = palette().color(QPalette::Text);
    const int interactionIndex =
        m_Hovered >= 0 ? m_Hovered : (hasFocus() ? m_Focused : -1);
    for(int i = 0; i < m_Turns.size(); ++i)
    {
      const int y = turnY(i);
      const int distance = interactionIndex < 0 ? 3 : qAbs(i - interactionIndex);
      const int length = distance == 0 ? 24 : (distance == 1 ? 20 : (distance == 2 ? 16 : 12));
      const int activeMix = distance == 0 ? 100 : (distance == 1 ? 66 : (distance == 2 ? 34 : 0));
      const QColor lineColor = blendedColor(normal, active, activeMix);
      const qreal width = distance == 0 ? 2.0 : (distance == 1 ? 1.7 : 1.3);
      QPen pen(lineColor, width, Qt::SolidLine, Qt::RoundCap);
      painter.setPen(pen);
      painter.drawLine(6, y, 6 + length, y);
    }
  }

  void mouseMoveEvent(QMouseEvent *event) override
  {
    const int hovered = turnAt(event->pos().y());
    if(hovered == m_Hovered)
    {
      if(hovered >= 0 && !m_PreviewPopup->isVisible())
        showPreview(hovered);
      return;
    }
    m_Hovered = hovered;
    updateAccessiblePreview(hasFocus() ? m_Focused : m_Hovered);
    update();
    if(m_Hovered >= 0)
    {
      setCursor(Qt::PointingHandCursor);
      showPreview(m_Hovered);
    }
    else
    {
      unsetCursor();
      m_PreviewPopup->hide();
    }
  }

  void leaveEvent(QEvent *) override
  {
    m_Hovered = -1;
    updateAccessiblePreview(hasFocus() ? m_Focused : -1);
    unsetCursor();
    m_PreviewPopup->hide();
    update();
  }

  void mousePressEvent(QMouseEvent *event) override
  {
    if(event->button() != Qt::LeftButton)
      return;
    const int selected = turnAt(event->pos().y());
    if(selected >= 0)
    {
      setFocus(Qt::MouseFocusReason);
      m_Focused = selected;
      updateAccessiblePreview(m_Focused);
      update();
      if(m_ActivateHandler)
        m_ActivateHandler(m_Turns[selected].messageId);
      m_PreviewPopup->hide();
      event->accept();
    }
  }

private:
  qreal railStep() const
  {
    if(m_Turns.size() <= 1)
      return 0.0;
    return qMin(12.0, qMax(1, height() - 20) / qreal(m_Turns.size() - 1));
  }

  qreal railTop() const
  {
    return (height() - qMax(0, m_Turns.size() - 1) * railStep()) / 2.0;
  }

  int turnY(int index) const { return qRound(railTop() + index * railStep()); }

  int turnAt(int y) const
  {
    if(m_Turns.isEmpty())
      return -1;
    if(m_Turns.size() == 1)
      return qAbs(y - turnY(0)) <= 7 ? 0 : -1;
    const qreal step = railStep();
    const int candidate = qBound(0, qRound((y - railTop()) / step), m_Turns.size() - 1);
    return qAbs(y - turnY(candidate)) <= qMax(3, int(step / 2.0 + 1.5)) ? candidate : -1;
  }

  void updateAccessiblePreview(int index)
  {
    QString description;
    if(index >= 0 && index < m_Turns.size())
    {
      description = m_Turns[index].userPreview;
      if(!m_Turns[index].assistantPreview.isEmpty())
      {
        // i18n-ignore: Blank lines separate two already-localized/dynamic accessible paragraphs.
        description += QLatin1String("\n\n") + m_Turns[index].assistantPreview;
      }
    }
    setAccessibleDescription(description);
  }

  void showPreview(int index)
  {
    if(index < 0 || index >= m_Turns.size() || !isActiveWindow() ||
       QApplication::applicationState() != Qt::ApplicationActive)
      return;

    const Turn &turn = m_Turns[index];
    const QPoint anchor = mapToGlobal(QPoint(width() + 7, turnY(index)));
    QRect available;
    if(QApplication::desktop())
      available = QApplication::desktop()->availableGeometry(anchor);
    if(!available.isValid())
      available = QRect(anchor - QPoint(200, 150), QSize(400, 300));

    const int popupHorizontalMargin = 8;
    const int popupVerticalMargin = 8;
    const int popupChromeWidth = 24;
    const int popupMaximumWidth = qMax(1, available.width() - popupHorizontalMargin * 2);
    const int popupMaximumHeight = qMax(1, available.height() - popupVerticalMargin * 2);
    const int contentWidth =
        qMax(1, qMin(360, popupMaximumWidth - popupChromeWidth));
    m_PreviewPopup->setPreview(turn.userPreview, turn.assistantPreview, contentWidth,
                               popupMaximumWidth, popupMaximumHeight);

    QPoint popupPosition(anchor.x(), anchor.y() - m_PreviewPopup->height() / 2);
    if(popupPosition.x() + m_PreviewPopup->width() > available.right() + 1)
      popupPosition.setX(mapToGlobal(QPoint(-7, 0)).x() - m_PreviewPopup->width());
    popupPosition.setX(qBound(available.left(), popupPosition.x(),
                              qMax(available.left(), available.right() + 1 -
                                                         m_PreviewPopup->width())));
    popupPosition.setY(qBound(available.top(), popupPosition.y(),
                              qMax(available.top(), available.bottom() + 1 -
                                                        m_PreviewPopup->height())));
    m_PreviewPopup->move(popupPosition);
    m_PreviewPopup->show();
    m_PreviewPopup->raise();
  }

  QVector<Turn> m_Turns;
  std::function<void(const QString &)> m_ActivateHandler;
  AITurnPreviewPopup *m_PreviewPopup = NULL;
  int m_Hovered = -1;
  int m_Focused = -1;
};

AIConversationView::AIConversationView(QWidget *parent) : QWidget(parent)
{
  QHBoxLayout *layout = new QHBoxLayout(this);
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(0);

  m_TurnRail = new AITurnRailWidget(this);
  m_TurnRail->setAccessibleName(
      QCoreApplication::translate("AIConversationView", "Conversation turns"));
  m_TurnRail->setActivateHandler([this](const QString &messageId) { scrollToMessage(messageId); });
  layout->addWidget(m_TurnRail);

  QWidget *browserContainer = new QWidget(this);
  QGridLayout *browserLayout = new QGridLayout(browserContainer);
  browserLayout->setContentsMargins(0, 0, 0, 0);
  browserLayout->setSpacing(0);

  m_Browser = new AIRoundedTextBrowser(browserContainer);
  m_Browser->setFrameShape(QFrame::NoFrame);
  m_Browser->setOpenLinks(false);
  m_Browser->setOpenExternalLinks(false);
  m_Browser->setReadOnly(true);
  m_Browser->setTextInteractionFlags(Qt::TextBrowserInteraction |
                                     Qt::TextSelectableByKeyboard);
  m_Browser->setFocusPolicy(Qt::StrongFocus);
  m_Browser->setContextMenuPolicy(Qt::CustomContextMenu);
  connect(m_Browser, &QTextBrowser::anchorClicked, this,
          [this](const QUrl &url) { activateLink(url); });
  connect(m_Browser, &QTextBrowser::customContextMenuRequested, this,
          [this](const QPoint &position) {
            QMenu *menu = m_Browser->createStandardContextMenu();
            const QString anchorHref = m_Browser->anchorAt(position);
            if(anchorHref.startsWith(QLatin1String("replaylab-annotation://edit/")))
            {
              menu->addSeparator();
              QAction *edit = menu->addAction(
                  QCoreApplication::translate("AIConversationView", "Edit marks in a new draft"));
              connect(edit, &QAction::triggered, this,
                      [this, anchorHref]() { activateLink(QUrl(anchorHref)); });
            }
            // i18n-ignore: Internal link scheme routed to the transcript link handler.
            if(anchorHref.startsWith(lit("replaylab-artifact://")) ||
               anchorHref.startsWith(lit("rdoc://shader"))) // i18n-ignore: Captured Shader navigation URI.
            {
              const QUrl artifactUrl(anchorHref);
              const QString artifactPath = artifactUrl.scheme() == QLatin1String("rdoc")
                  ? QUrlQuery(artifactUrl).queryItemValue(QStringLiteral("path"), QUrl::FullyDecoded)
                  : QUrl::fromPercentEncoding(artifactUrl.path().mid(1).toUtf8());
              menu->addSeparator();
              QAction *open = menu->addAction(
                  QCoreApplication::translate("AIConversationView", "Open file"));
              connect(open, &QAction::triggered, this, [this, artifactUrl]() { activateLink(artifactUrl); });
              QAction *copy = menu->addAction(
                  QCoreApplication::translate("AIConversationView", "Copy path"));
              connect(copy, &QAction::triggered, this,
                      [artifactPath]() { QGuiApplication::clipboard()->setText(artifactPath); });
              QAction *reveal = menu->addAction(
                  QCoreApplication::translate("AIConversationView", "Show in folder"));
              connect(reveal, &QAction::triggered, this, [this, artifactPath]() {
                // i18n-ignore: Internal link scheme routed to the transcript link handler.
                activateLink(QUrl(lit("replaylab-artifact://reveal/") +
                                  QString::fromUtf8(QUrl::toPercentEncoding(artifactPath))));
              });
            }
            if(m_SelectionAttachmentHandler && m_Browser->textCursor().hasSelection())
            {
              menu->addSeparator();
              QAction *add = menu->addAction(
                  QCoreApplication::translate("AIConversationView", "Add to chat"));
              connect(add, &QAction::triggered, this,
                      [this]() { addSelectedTextToAttachment(); });
            }
            menu->exec(m_Browser->viewport()->mapToGlobal(position));
            delete menu;
          });
  browserLayout->addWidget(m_Browser, 0, 0);

  m_SelectionAction = new QToolButton(m_Browser->viewport());
  m_SelectionAction->setAutoRaise(false);
  m_SelectionAction->setFocusPolicy(Qt::NoFocus);
  m_SelectionAction->setIcon(Icons::text_add());
  m_SelectionAction->setText(
      QCoreApplication::translate("AIConversationView", "Add to chat"));
  m_SelectionAction->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
  m_SelectionAction->setCursor(Qt::PointingHandCursor);
  m_SelectionAction->setAccessibleName(
      QCoreApplication::translate("AIConversationView", "Add selected text to chat"));
  // i18n-ignore: The stylesheet contains no user-visible prose.
  m_SelectionAction->setStyleSheet(lit(
      "QToolButton { border: 1px solid palette(mid); border-radius: 14px; "
      "background: palette(base); padding: 4px 10px; } "
      "QToolButton:hover { background: palette(midlight); }"));
  m_SelectionAction->hide();
  connect(m_Browser, &QTextBrowser::selectionChanged, this,
          [this]() {
            if(m_Browser && m_Browser->textCursor().hasSelection())
            {
              if(!m_AdjustingScroll)
                ++m_UserInteractionGeneration;
              detachFromTail();
            }
            QTimer::singleShot(0, this, [this]() {
              updateSelectionAction();
              flushDeferredRender();
            });
          });
  connect(m_SelectionAction, &QToolButton::clicked, this,
          [this]() { addSelectedTextToAttachment(); });

  m_ScrollToLatest = new QToolButton(browserContainer);
  m_ScrollToLatest->setArrowType(Qt::DownArrow);
  m_ScrollToLatest->setAutoRaise(false);
  m_ScrollToLatest->setFixedSize(34, 34);
  m_ScrollToLatest->setToolTip(
      QCoreApplication::translate("AIConversationView", "Jump to latest message"));
  m_ScrollToLatest->setAccessibleName(
      QCoreApplication::translate("AIConversationView", "Jump to latest message"));
  // i18n-ignore: The stylesheet contains no user-visible prose.
  m_ScrollToLatest->setStyleSheet(lit(
      "QToolButton { border: 1px solid palette(mid); border-radius: 17px; "
      "background: palette(base); } QToolButton:hover { background: palette(midlight); }"));
  browserLayout->addWidget(m_ScrollToLatest, 0, 0, Qt::AlignHCenter | Qt::AlignBottom);
  m_ScrollToLatest->hide();
  connect(m_ScrollToLatest, &QToolButton::clicked, this, [this]() { scrollToLatest(); });

  QScrollBar *scroll = m_Browser->verticalScrollBar();
  connect(scroll, &QScrollBar::valueChanged, this, [this](int) { updateVisualReplies(); });
  // valueChanged is also emitted when QTextDocument's lazy layout changes and clamps the scroll
  // range. Treat only slider actions and input events as user intent so a layout pass cannot
  // silently re-enable tail following while the user is reading earlier content.
  connect(scroll, &QAbstractSlider::actionTriggered, this, [this](int action) {
    ++m_UserInteractionGeneration;
    if(action == QAbstractSlider::SliderSingleStepSub ||
       action == QAbstractSlider::SliderPageStepSub ||
       action == QAbstractSlider::SliderToMinimum)
      detachFromTail();
    scheduleUserScrollUpdate();
  });
  connect(scroll, &QAbstractSlider::sliderMoved, this,
          [this, scroll](int value) {
            ++m_UserInteractionGeneration;
            if(value < scroll->maximum() - 2)
              detachFromTail();
            scheduleUserScrollUpdate();
          });
  connect(scroll, &QAbstractSlider::sliderReleased, this,
          [this]() { scheduleUserScrollUpdate(); });
  connect(scroll, &QScrollBar::rangeChanged, this, [this](int, int) {
    if(m_AdjustingScroll)
      return;
    if(m_FollowTail)
      scrollToLatest();
    else
    {
      scheduleViewportAnchorRestore();
      updateTailControl();
    }
  });
  m_Browser->installEventFilter(this);
  m_Browser->viewport()->installEventFilter(this);
  scroll->installEventFilter(this);

  layout->addWidget(browserContainer, 1);

  m_LiveTextTimer = new QTimer(this);
  m_LiveTextTimer->setInterval(LiveTextRevealIntervalMs);
  m_LiveTextTimer->setTimerType(Qt::PreciseTimer);
  connect(m_LiveTextTimer, &QTimer::timeout, this, [this]() { advanceLiveText(); });
  m_ElapsedTimer = new QTimer(this);
  m_ElapsedTimer->setInterval(1000);
  connect(m_ElapsedTimer, &QTimer::timeout, this, [this]() { advanceElapsedText(); });
}

void AIConversationView::rebuildElapsedCursors()
{
  m_ElapsedCursors.clear();
  if(!m_Browser || !m_Browser->document() || m_ElapsedAnchors.isEmpty())
  {
    if(m_ElapsedTimer)
      m_ElapsedTimer->stop();
    return;
  }
  QTextDocument *document = m_Browser->document();
  for(QTextBlock block = document->begin(); block.isValid(); block = block.next())
  {
    for(QTextBlock::iterator it = block.begin(); !it.atEnd(); ++it)
    {
      const QTextFragment fragment = it.fragment();
      if(!fragment.isValid())
        continue;
      for(const QString &name : fragment.charFormat().anchorNames())
      {
        const auto anchor = m_ElapsedAnchors.constFind(name);
        if(anchor == m_ElapsedAnchors.constEnd() || m_ElapsedCursors.contains(name))
          continue;
        // Qt's HTML importer assigns a named anchor only to the first character,
        // not the whole <a> contents. Use the exact rendered label length: neither
        // that fragment nor a merge of same-name fragments includes the unit.
        ElapsedCursor target;
        target.cursor = QTextCursor(document);
        target.cursor.setPosition(fragment.position());
        target.format = fragment.charFormat();
        target.text = anchor.value().second;
        target.length = target.text.size();
        target.startMs = anchor.value().first;
        if(target.length == 0 || fragment.position() + target.length >= document->characterCount())
          continue;
        QTextCursor span(target.cursor);
        span.setPosition(fragment.position() + target.length, QTextCursor::KeepAnchor);
        if(span.selectedText() != target.text)
          continue;
        m_ElapsedCursors.insert(name, target);
      }
    }
  }
  if(m_ElapsedTimer)
  {
    if(m_ElapsedCursors.isEmpty())
      m_ElapsedTimer->stop();
    else if(!m_ElapsedTimer->isActive())
      m_ElapsedTimer->start();
  }
}

void AIConversationView::advanceElapsedText()
{
  // Paint changing durations over their stable spans. Never edit the rich-text
  // document from a clock tick: streamed tables and lazy layout can otherwise
  // leave Qt 5.9's incremental layout with an invalid QTextLine.
  if(!m_Browser || !m_Browser->document() || m_ElapsedCursors.isEmpty())
  {
    if(m_Browser)
      static_cast<AIRoundedTextBrowser *>(m_Browser)->setElapsedOverlays({});
    if(m_ElapsedTimer)
      m_ElapsedTimer->stop();
    return;
  }
  const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
  bool stale = false;
  QVector<AIRoundedTextBrowser::ElapsedOverlay> overlays;
  for(auto it = m_ElapsedCursors.begin(); it != m_ElapsedCursors.end(); ++it)
  {
    ElapsedCursor &entry = it.value();
    const QString text = elapsedText(nowMs - entry.startMs);
    if(text.size() == 0 || entry.cursor.isNull())
      continue;
    const int position = entry.cursor.position();
    QTextCursor edit(m_Browser->document());
    edit.setPosition(position);
    edit.setPosition(position + entry.length, QTextCursor::KeepAnchor);
    const QString current = edit.selectedText();
    // A structural render or live insertion may have moved the span. Resolve it
    // again rather than painting over unrelated text.
    if(current != entry.text)
    {
      stale = true;
      continue;
    }
    AIRoundedTextBrowser::ElapsedOverlay overlay;
    overlay.cursor = entry.cursor;
    overlay.length = entry.length;
    overlay.format = entry.format;
    overlay.text = text;
    overlays.push_back(overlay);
  }
  static_cast<AIRoundedTextBrowser *>(m_Browser)->setElapsedOverlays(overlays);
  if(stale)
    rebuildElapsedCursors();
}

void AIConversationView::setMessages(const QVector<AIChatMessage> &messages)
{
  QHash<QString, QString> oldLiveTargets;
  QHash<QString, quint64> oldLiveRevisions;
  QHash<QString, int> oldLiveVisible;
  for(const AIChatMessage &message : m_Messages)
  {
    for(const AIActivityItem &activity : message.activity)
    {
      if(!activity.liveText)
        continue;
      const QString key = liveTextKey(message, activity);
      oldLiveTargets.insert(key, activity.detail);
      oldLiveRevisions.insert(key, activity.liveTextRevision);
      oldLiveVisible.insert(key, activity.visibleDetailChars);
    }
  }

  bool hadLive = false;
  for(const AIChatMessage &message : m_Messages)
    hadLive |= isLiveState(message.state) && message.timestampMs > 0;
  const QString oldConversationKey =
      m_Messages.isEmpty() ? QString() : m_Messages.constFirst().id;
  const QString newConversationKey = messages.isEmpty() ? QString() : messages.constFirst().id;
  const bool conversationChanged = oldConversationKey != newConversationKey;
  const bool keepDocument =
      !conversationChanged && canKeepTranscriptDocument(m_Messages, messages);
  if(conversationChanged)
  {
    ++m_UserInteractionGeneration;
    ++m_TailResetGeneration;
    m_FollowTail = true;
    m_PreservedScrollValue = 0;
    clearViewportAnchor();
    m_RenderPendingWhileDetached = false;
    m_RenderPendingForSelection = false;
    updateTailControl();
  }
  m_Messages = messages;
  for(AIChatMessage &message : m_Messages)
  {
    for(AIActivityItem &activity : message.activity)
    {
      if(!activity.liveText)
        continue;
      const QString key = liveTextKey(message, activity);
      if(conversationChanged)
      {
        // Switching conversations materializes existing history immediately. Later extensions to
        // the same live stream are coalesced into one insertion per display frame.
        activity.visibleDetailChars = -1;
        continue;
      }

      const auto oldTarget = oldLiveTargets.constFind(key);
      if(oldTarget == oldLiveTargets.constEnd() ||
         oldLiveRevisions.value(key) != activity.liveTextRevision ||
         !activity.detail.startsWith(oldTarget.value()))
      {
        activity.visibleDetailChars = activity.detail.isEmpty() ? -1 : 0;
        continue;
      }

      const int previousVisible = oldLiveVisible.value(key, -1) < 0
                                      ? oldTarget.value().size()
                                      : oldLiveVisible.value(key);
      activity.visibleDetailChars = qMin(previousVisible, activity.detail.size());
      if(activity.visibleDetailChars >= activity.detail.size())
        activity.visibleDetailChars = -1;
    }
  }

  const bool selectionActive =
      !conversationChanged &&
      (m_SelectingText || (m_Browser && m_Browser->textCursor().hasSelection()));
  if(selectionActive && !keepDocument)
    m_RenderPendingForSelection = true;
  if(selectionActive && m_RenderPendingForSelection)
  {
    // A reset/new row/terminal state needs a structural render, but replacing QTextDocument while
    // text is selected makes copying impossible. Freeze the reveal target and render the latest
    // model once the selection is cleared.
    if(m_LiveTextTimer)
      m_LiveTextTimer->stop();
    updateTailControl();
    return;
  }
  const bool flushSelectionRender = m_RenderPendingForSelection;
  if(flushSelectionRender)
    m_RenderPendingForSelection = false;

  if(keepDocument && !flushSelectionRender)
  {
    // The rich-text structure is unchanged. Replacing it here would discard the user's selection
    // and reset the scrollbar even though only a live target gained a suffix.
    updateLiveTextTimer();
    updateTailControl();
    return;
  }

  bool hasLive = false;
  for(const AIChatMessage &message : m_Messages)
    hasLive |= isLiveState(message.state) && message.timestampMs > 0;
  if(!conversationChanged && !m_FollowTail && !hadLive && !hasLive)
  {
    // Static history can remain materialized while the user reads it. A live update, including the
    // live-to-terminal transition, always renders through the viewport-anchor preservation path.
    m_RenderPendingWhileDetached = true;
    updateTailControl();
    updateLiveTextTimer();
    return;
  }
  render();
}

void AIConversationView::clear()
{
  clearVisualReplies();
  ++m_RenderGeneration;
  ++m_TailResetGeneration;
  m_Messages.clear();
  m_MessageAnchors.clear();
  m_LiveTextAnchors.clear();
  m_LiveTextCursors.clear();
  m_ElapsedAnchors.clear();
  m_ElapsedCursors.clear();
  m_FollowTail = true;
  m_AdjustingScroll = false;
  m_SelectingText = false;
  ++m_UserInteractionGeneration;
  m_PreservedScrollValue = 0;
  clearViewportAnchor();
  m_RenderPendingWhileDetached = false;
  m_RenderPendingForSelection = false;
  if(m_TurnRail)
    m_TurnRail->setTurns(QVector<AITurnRailWidget::Turn>());
  if(m_Browser)
  {
    static_cast<AIRoundedTextBrowser *>(m_Browser)->setElapsedOverlays({});
    m_Browser->clear();
  }
  if(m_SelectionAction)
    m_SelectionAction->hide();
  updateTailControl();
  if(m_LiveTextTimer)
    m_LiveTextTimer->stop();
}

void AIConversationView::updateLiveTextTimer()
{
  bool pending = false;
  for(const AIChatMessage &message : m_Messages)
  {
    for(const AIActivityItem &activity : message.activity)
    {
      if(activity.liveText && activity.visibleDetailChars >= 0 &&
         activity.visibleDetailChars < activity.detail.size())
      {
        pending = true;
        break;
      }
    }
    if(pending)
      break;
  }

  if(!m_LiveTextTimer)
    return;
  if(pending && !m_LiveTextTimer->isActive())
    m_LiveTextTimer->start();
  else if(!pending && m_LiveTextTimer->isActive())
    m_LiveTextTimer->stop();
}

void AIConversationView::advanceLiveText()
{
  bool needsRender = false;
  for(AIChatMessage &message : m_Messages)
  {
    for(AIActivityItem &activity : message.activity)
    {
      if(!activity.liveText || activity.visibleDetailChars < 0 ||
         activity.visibleDetailChars >= activity.detail.size())
        continue;

      // Display the complete received suffix in one edit. The provider already streams bounded
      // text fragments; animating those again one grapheme at a time both delayed visible progress
      // and repeatedly segmented the entire growing string on the GUI thread. Appending a suffix
      // intact preserves all Unicode text without introducing artificial grapheme boundaries.
      const QString suffix = activity.detail.mid(activity.visibleDetailChars);
      const QString key = liveTextKey(message, activity);
      auto cursor = m_LiveTextCursors.find(key);
      if(cursor != m_LiveTextCursors.end())
      {
        cursor.value().cursor.insertText(suffix, cursor.value().format);
      }
      else if(message.activityExpanded)
      {
        // A structural render may have replaced the QTextDocument between timer ticks. Rebuild its
        // stable tail cursor before advancing this visible stream.
        needsRender = true;
        continue;
      }

      activity.visibleDetailChars = -1;
    }
  }

  if(needsRender)
  {
    const bool selectionActive =
        m_SelectingText || (m_Browser && m_Browser->textCursor().hasSelection());
    if(selectionActive)
    {
      m_RenderPendingForSelection = true;
      if(m_LiveTextTimer)
        m_LiveTextTimer->stop();
    }
    else
    {
      render();
    }
  }
  else
    updateLiveTextTimer();
}

void AIConversationView::rebuildLiveTextCursors()
{
  m_LiveTextCursors.clear();
  if(!m_Browser || !m_Browser->document() || m_LiveTextAnchors.isEmpty())
    return;

  QHash<QString, QString> keyByAnchor;
  for(auto it = m_LiveTextAnchors.constBegin(); it != m_LiveTextAnchors.constEnd(); ++it)
    keyByAnchor.insert(it.value(), it.key());

  QTextDocument *document = m_Browser->document();
  for(QTextBlock block = document->begin(); block.isValid(); block = block.next())
  {
    for(QTextBlock::iterator it = block.begin(); !it.atEnd(); ++it)
    {
      const QTextFragment fragment = it.fragment();
      if(!fragment.isValid())
        continue;
      for(const QString &name : fragment.charFormat().anchorNames())
      {
        const auto key = keyByAnchor.constFind(name);
        if(key == keyByAnchor.constEnd())
          continue;
        LiveTextCursor target;
        target.cursor = QTextCursor(document);
        target.cursor.setPosition(fragment.position());
        target.format = fragment.charFormat();
        target.format.setAnchor(false);
        target.format.setAnchorHref(QString());
        target.format.setAnchorNames(QStringList());
        target.format.setForeground(palette().brush(QPalette::Disabled, QPalette::Text));
        target.format.setFontUnderline(false);
        m_LiveTextCursors.insert(key.value(), target);
      }
    }
  }
}

void AIConversationView::scrollToMessage(const QString &messageId)
{
  if(!m_Browser)
    return;
  const QString anchor = m_MessageAnchors.value(messageId);
  if(!anchor.isEmpty())
  {
    m_Browser->scrollToAnchor(anchor);
    updateFollowTailFromUser();
  }
}

void AIConversationView::followSubmittedTurn()
{
  // Sending is an explicit request to return to the live conversation. A selection in the old
  // answer survives focus moving to the composer; leaving it in place would defer every new row
  // indefinitely. Do not do this on background updates, which must preserve reading/copying.
  ++m_UserInteractionGeneration;
  ++m_TailResetGeneration;
  m_SelectingText = false;
  m_FollowTail = true;
  clearViewportAnchor();
  if(m_Browser)
  {
    const bool wasAdjusting = m_AdjustingScroll;
    m_AdjustingScroll = true;
    QTextCursor cursor = m_Browser->textCursor();
    cursor.clearSelection();
    m_Browser->setTextCursor(cursor);
    m_AdjustingScroll = wasAdjusting;
  }
  updateSelectionAction();
  scrollToLatest();
}

void AIConversationView::scrollToLatest()
{
  QScrollBar *bar = m_Browser ? m_Browser->verticalScrollBar() : NULL;
  m_FollowTail = true;
  clearViewportAnchor();
  if(m_RenderPendingForSelection)
  {
    m_RenderPendingForSelection = false;
    updateTailControl();
    render(false);
    return;
  }
  if(m_RenderPendingWhileDetached)
  {
    updateTailControl();
    render();
    return;
  }
  if(bar)
  {
    const bool wasAdjusting = m_AdjustingScroll;
    m_AdjustingScroll = true;
    bar->setValue(bar->maximum());
    m_AdjustingScroll = wasAdjusting;
  }
  updateTailControl();
}

void AIConversationView::detachFromTail()
{
  if(!m_FollowTail)
    return;
  QScrollBar *bar = m_Browser ? m_Browser->verticalScrollBar() : NULL;
  m_FollowTail = false;
  if(bar)
    m_PreservedScrollValue = bar->value();
  captureViewportAnchor();
  updateTailControl();
}

void AIConversationView::updateTailControl()
{
  if(!m_ScrollToLatest)
    return;
  QScrollBar *bar = m_Browser ? m_Browser->verticalScrollBar() : NULL;
  const bool canScroll = bar && bar->maximum() > bar->minimum();
  m_ScrollToLatest->setVisible(!m_FollowTail && (canScroll || m_RenderPendingWhileDetached));
  if(m_ScrollToLatest->isVisible())
    m_ScrollToLatest->raise();
}

void AIConversationView::scheduleUserScrollUpdate()
{
  if(m_UserScrollUpdatePending)
    return;
  m_UserScrollUpdatePending = true;
  const quint64 tailResetGeneration = m_TailResetGeneration;
  QTimer::singleShot(0, this, [this, tailResetGeneration]() {
    m_UserScrollUpdatePending = false;
    if(tailResetGeneration == m_TailResetGeneration)
      updateFollowTailFromUser();
  });
}

void AIConversationView::updateFollowTailFromUser()
{
  if(m_AdjustingScroll)
    return;
  QScrollBar *bar = m_Browser ? m_Browser->verticalScrollBar() : NULL;
  if(!bar)
    return;

  m_FollowTail = bar->maximum() - bar->value() <= 2;
  if(m_FollowTail)
  {
    clearViewportAnchor();
    if(m_RenderPendingWhileDetached)
    {
      updateTailControl();
      render();
      return;
    }
  }
  else
  {
    m_PreservedScrollValue = bar->value();
    captureViewportAnchor();
  }
  updateTailControl();
}

void AIConversationView::clearViewportAnchor()
{
  m_ViewportAnchor = ViewportAnchor();
}

void AIConversationView::captureViewportAnchor()
{
  if(m_FollowTail || !m_Browser || !m_Browser->document())
    return;

  QScrollBar *bar = m_Browser->verticalScrollBar();
  if(bar)
    m_PreservedScrollValue = bar->value();

  // Sample the horizontal centre so right-aligned user bubbles do not resolve to the empty table
  // cell on their left.
  const int anchorX = qMax(1, m_Browser->viewport()->width() / 2);
  QTextCursor cursor = m_Browser->cursorForPosition(QPoint(anchorX, 1));
  const QTextBlock block = cursor.block();
  if(!block.isValid())
  {
    clearViewportAnchor();
    return;
  }

  ViewportAnchor anchor;
  anchor.valid = true;
  anchor.cursorInBlock = qBound(0, cursor.position() - block.position(), block.length() - 1);
  anchor.viewportY = m_Browser->cursorRect(cursor).top();

  const QString blockText = block.text();
  anchor.blockTextIsComplete = blockText.size() <= ViewportAnchorCompleteBlockChars;
  if(anchor.blockTextIsComplete)
    anchor.blockText = blockText;

  const int maximumContextStart = qMax(0, blockText.size() - ViewportAnchorContextChars);
  const int contextStart =
      qBound(0, anchor.cursorInBlock - ViewportAnchorContextChars / 2, maximumContextStart);
  anchor.context = blockText.mid(contextStart, ViewportAnchorContextChars);
  anchor.contextCursorOffset = anchor.cursorInBlock - contextStart;

  const QVector<MessageDocumentAnchor> positions =
      messageDocumentAnchors(m_Browser->document(), m_MessageAnchors);
  int messageStart = 0;
  for(const MessageDocumentAnchor &position : positions)
  {
    if(position.position > cursor.position())
      break;
    anchor.messageId = position.messageId;
    messageStart = position.position;
  }
  const QTextBlock messageStartBlock = m_Browser->document()->findBlock(messageStart);
  anchor.relativeBlock =
      qMax(0, block.blockNumber() - (messageStartBlock.isValid() ? messageStartBlock.blockNumber() : 0));
  m_ViewportAnchor = anchor;
}

void AIConversationView::scheduleViewportAnchorRestore()
{
  if(m_FollowTail || m_ViewportAnchorRestorePending)
    return;
  m_ViewportAnchorRestorePending = true;
  const int renderGeneration = m_RenderGeneration;
  const quint64 userInteractionGeneration = m_UserInteractionGeneration;
  QTimer::singleShot(0, this, [this, renderGeneration, userInteractionGeneration]() {
    m_ViewportAnchorRestorePending = false;
    if(renderGeneration == m_RenderGeneration &&
       userInteractionGeneration == m_UserInteractionGeneration && !m_FollowTail)
      restoreViewportAnchor();
  });
}

void AIConversationView::restoreViewportAnchor()
{
  if(m_FollowTail || !m_Browser || !m_Browser->document())
    return;
  QScrollBar *bar = m_Browser->verticalScrollBar();
  if(!bar)
    return;

  if(!m_ViewportAnchor.valid)
  {
    const bool wasAdjusting = m_AdjustingScroll;
    m_AdjustingScroll = true;
    bar->setValue(qMin(m_PreservedScrollValue, bar->maximum()));
    m_AdjustingScroll = wasAdjusting;
    updateTailControl();
    return;
  }

  QTextDocument *document = m_Browser->document();
  const QVector<MessageDocumentAnchor> positions =
      messageDocumentAnchors(document, m_MessageAnchors);
  int scopeStart = 0;
  int scopeEnd = document->characterCount();
  if(!m_ViewportAnchor.messageId.isEmpty())
  {
    for(int i = 0; i < positions.size(); ++i)
    {
      if(positions[i].messageId != m_ViewportAnchor.messageId)
        continue;
      scopeStart = positions[i].position;
      if(i + 1 < positions.size())
        scopeEnd = positions[i + 1].position;
      break;
    }
  }

  QTextBlock scopeFirstBlock = document->findBlock(scopeStart);
  if(!scopeFirstBlock.isValid())
    scopeFirstBlock = document->begin();
  const int expectedBlockNumber = scopeFirstBlock.blockNumber() + m_ViewportAnchor.relativeBlock;

  QTextBlock bestBlock;
  int bestCursorInBlock = 0;
  int bestRank = 3;
  int bestDistance = 0x7fffffff;
  QTextBlock fallbackBlock;
  for(QTextBlock block = scopeFirstBlock; block.isValid() && block.position() < scopeEnd;
      block = block.next())
  {
    const int distance = qAbs(block.blockNumber() - expectedBlockNumber);
    if(!fallbackBlock.isValid() || distance <
                                     qAbs(fallbackBlock.blockNumber() - expectedBlockNumber))
      fallbackBlock = block;

    const QString text = block.text();
    int rank = 3;
    int cursorInBlock = qMin(m_ViewportAnchor.cursorInBlock, text.size());
    if(m_ViewportAnchor.blockTextIsComplete && text == m_ViewportAnchor.blockText)
    {
      rank = 0;
    }
    else if(!m_ViewportAnchor.context.isEmpty())
    {
      int match = text.indexOf(m_ViewportAnchor.context);
      if(match >= 0)
      {
        int closestMatch = match;
        while(match >= 0)
        {
          if(qAbs(match + m_ViewportAnchor.contextCursorOffset -
                  m_ViewportAnchor.cursorInBlock) <
             qAbs(closestMatch + m_ViewportAnchor.contextCursorOffset -
                  m_ViewportAnchor.cursorInBlock))
            closestMatch = match;
          match = text.indexOf(m_ViewportAnchor.context, match + 1);
        }
        cursorInBlock = closestMatch + m_ViewportAnchor.contextCursorOffset;
        rank = 1;
      }
    }

    if(rank < bestRank || (rank == bestRank && distance < bestDistance))
    {
      bestRank = rank;
      bestDistance = distance;
      bestBlock = block;
      bestCursorInBlock = cursorInBlock;
    }
  }

  if(!bestBlock.isValid() || bestRank >= 3)
  {
    bestBlock = fallbackBlock;
    if(bestBlock.isValid())
      bestCursorInBlock = qMin(m_ViewportAnchor.cursorInBlock, bestBlock.text().size());
  }
  if(!bestBlock.isValid())
    return;

  QTextCursor cursor(document);
  cursor.setPosition(bestBlock.position() + bestCursorInBlock);
  const bool wasAdjusting = m_AdjustingScroll;
  m_AdjustingScroll = true;
  const int cursorViewportY = m_Browser->cursorRect(cursor).top();
  const int target = bar->value() + cursorViewportY - m_ViewportAnchor.viewportY;
  bar->setValue(qBound(bar->minimum(), target, bar->maximum()));
  m_PreservedScrollValue = bar->value();
  m_AdjustingScroll = wasAdjusting;
  updateTailControl();
}

void AIConversationView::setLinkHandler(std::function<void(const QUrl &)> handler)
{
  m_LinkHandler = std::move(handler);
}

void AIConversationView::setVisualLinkHandler(
    std::function<void(const QUrl &, const QJsonObject &)> handler)
{
  m_VisualLinkHandler = std::move(handler);
}

void AIConversationView::clearVisualReplies()
{
  qDeleteAll(m_VisualReplies);
  m_VisualReplies.clear();
  m_VisualPositions.clear();
}

void AIConversationView::updateVisualReplies()
{
  if(!m_Browser)
    return;
  const QRect viewport = m_Browser->viewport()->rect();
  for(auto it = m_VisualPositions.begin(); it != m_VisualPositions.end(); ++it)
  {
    QTextCursor cursor = it.value();
    if(cursor.isNull())
      continue;
    QTextImageFormat format = cursor.charFormat().toImageFormat();
    const int width = qMax(180, viewport.width() - 28);
    const int height = qBound(220, int(format.height()), 600);
    if(int(format.width()) != width)
    {
      format.setWidth(width);
      cursor.setCharFormat(format);
    }
    QTextCursor start = cursor;
    start.setPosition(cursor.selectionStart());
    QRect area = m_Browser->cursorRect(start);
    area.setSize(QSize(width, height));
    const bool visible = area.intersects(viewport);
    AIVisualReply *widget = m_VisualReplies.value(it.key(), NULL);
    if(!widget && visible)
    {
      const QString identity = it.key().section(QLatin1Char(':'), -1);
      widget = new AIVisualReply(
          identity,
          [this](const QUrl &url, const QJsonObject &capture) {
            if(m_VisualLinkHandler)
              m_VisualLinkHandler(url, capture);
          },
          m_Browser->viewport());
      m_VisualReplies.insert(it.key(), widget);
    }
    if(widget)
    {
      widget->setGeometry(area);
      widget->setVisible(visible);
      if(visible)
        widget->raise();
    }
  }
  // Bound retained renderers while preserving nearby camera/divider state.
  for(auto it = m_VisualReplies.begin(); it != m_VisualReplies.end();)
  {
    if(!m_VisualPositions.contains(it.key()) ||
       (m_VisualReplies.size() > 12 && !it.value()->isVisible()))
    {
      delete it.value();
      it = m_VisualReplies.erase(it);
    }
    else
      ++it;
  }
}

void AIConversationView::setActivityToggleHandler(
    std::function<void(const QString &messageId, bool expanded)> handler)
{
  m_ActivityToggleHandler = std::move(handler);
}

void AIConversationView::setSelectionAttachmentHandler(
    std::function<void(const QString &text)> handler)
{
  m_SelectionAttachmentHandler = std::move(handler);
  updateSelectionAction();
}

void AIConversationView::addSelectedTextToAttachment()
{
  if(!m_Browser || !m_SelectionAttachmentHandler)
    return;
  QString text = m_Browser->textCursor().selectedText();
  text.replace(QChar::ParagraphSeparator, QLatin1Char('\n'));
  text.replace(QChar::LineSeparator, QLatin1Char('\n'));
  text.remove(QChar(0x200b));
  if(!text.trimmed().isEmpty())
    m_SelectionAttachmentHandler(text);
  if(m_SelectionAction)
    m_SelectionAction->hide();
}

void AIConversationView::flushDeferredRender()
{
  if(!m_RenderPendingForSelection || m_SelectingText || !m_Browser ||
     m_Browser->textCursor().hasSelection())
    return;
  m_RenderPendingForSelection = false;
  render();
}

void AIConversationView::updateSelectionAction()
{
  if(!m_Browser || !m_SelectionAction || !m_SelectionAttachmentHandler)
  {
    if(m_SelectionAction)
      m_SelectionAction->hide();
    return;
  }

  QTextCursor cursor = m_Browser->textCursor();
  if(!cursor.hasSelection() || cursor.selectedText().trimmed().isEmpty())
  {
    m_SelectionAction->hide();
    return;
  }

  cursor.setPosition(cursor.selectionEnd());
  const QRect caret = m_Browser->cursorRect(cursor);
  m_SelectionAction->adjustSize();
  const QSize buttonSize = m_SelectionAction->size();
  const QRect available = m_Browser->viewport()->rect().adjusted(4, 4, -4, -4);
  int x = caret.right() - buttonSize.width();
  int y = caret.bottom() + 6;
  if(y + buttonSize.height() > available.bottom())
    y = caret.top() - buttonSize.height() - 6;
  x = qBound(available.left(), x,
             qMax(available.left(), available.right() - buttonSize.width() + 1));
  y = qBound(available.top(), y,
             qMax(available.top(), available.bottom() - buttonSize.height() + 1));
  m_SelectionAction->move(x, y);
  m_SelectionAction->show();
  m_SelectionAction->raise();
}

void AIConversationView::changeEvent(QEvent *event)
{
  QWidget::changeEvent(event);
  if(event && (event->type() == QEvent::PaletteChange || event->type() == QEvent::StyleChange) &&
     !m_Messages.isEmpty())
  {
    const bool selectionActive =
        m_SelectingText || (m_Browser && m_Browser->textCursor().hasSelection());
    if(selectionActive)
    {
      m_RenderPendingForSelection = true;
      if(m_LiveTextTimer)
        m_LiveTextTimer->stop();
    }
    else
    {
      render();
    }
  }
}

bool AIConversationView::eventFilter(QObject *watched, QEvent *event)
{
  if(event && m_Browser && watched == m_Browser->viewport() &&
     (event->type() == QEvent::Resize || event->type() == QEvent::Show))
    QTimer::singleShot(0, this, [this]() { updateVisualReplies(); });
  if(event && m_Browser &&
     (watched == m_Browser || watched == m_Browser->viewport() ||
      watched == m_Browser->verticalScrollBar()))
  {
    if(event->type() == QEvent::ShortcutOverride)
    {
      QKeyEvent *key = static_cast<QKeyEvent *>(event);
      if(key->matches(QKeySequence::Copy) && m_Browser->textCursor().hasSelection())
      {
        event->accept();
        return true;
      }
    }
    bool userScrollInput = event->type() == QEvent::Wheel || event->type() == QEvent::Scroll;
    bool detachInput = event->type() == QEvent::Scroll;
    if(event->type() == QEvent::Wheel)
    {
      const QWheelEvent *wheel = static_cast<QWheelEvent *>(event);
      const int verticalDelta =
          wheel->angleDelta().y() != 0 ? wheel->angleDelta().y() : wheel->pixelDelta().y();
      detachInput = verticalDelta > 0;
    }
    if(event->type() == QEvent::KeyPress)
    {
      const QKeyEvent *key = static_cast<QKeyEvent *>(event);
      if(key->matches(QKeySequence::Copy) && m_Browser->textCursor().hasSelection())
      {
        m_Browser->copy();
        return true;
      }
      switch(key->key())
      {
        case Qt::Key_Down:
        case Qt::Key_PageDown:
        case Qt::Key_End:
        case Qt::Key_Space:
          userScrollInput = true;
          detachInput = (key->modifiers() & Qt::ShiftModifier) != 0;
          break;
        case Qt::Key_Up:
        case Qt::Key_PageUp:
        case Qt::Key_Home:
          userScrollInput = true;
          detachInput = true;
          break;
        default: break;
      }
    }
    if(watched == m_Browser->viewport() && event->type() == QEvent::MouseButtonPress &&
       static_cast<QMouseEvent *>(event)->button() == Qt::LeftButton)
    {
      ++m_UserInteractionGeneration;
      m_SelectingText = true;
      detachInput = true;
    }
    if(userScrollInput)
      ++m_UserInteractionGeneration;
    if(detachInput)
      detachFromTail();
    if(userScrollInput)
    {
      scheduleUserScrollUpdate();
      if(m_SelectionAction)
        m_SelectionAction->hide();
      QTimer::singleShot(0, this, [this]() { updateSelectionAction(); });
    }
    if(event->type() == QEvent::MouseButtonRelease && m_SelectingText)
    {
      m_SelectingText = false;
      QTimer::singleShot(0, this, [this]() {
        updateSelectionAction();
        flushDeferredRender();
        if(m_Browser && !m_Browser->textCursor().hasSelection())
          scheduleUserScrollUpdate();
      });
    }
    else if(event->type() == QEvent::FocusOut && m_SelectionAction)
    {
      m_SelectingText = false;
      m_SelectionAction->hide();
    }
  }
  return QWidget::eventFilter(watched, event);
}

void AIConversationView::activateLink(const QUrl &url)
{
  if(url.scheme() == QLatin1String("replaylab-activity") && url.host() == QLatin1String("more"))
  {
    const QString id = QUrl::fromPercentEncoding(url.path().mid(1).toUtf8());
    m_ActivityHistoryLimits[id] = m_ActivityHistoryLimits.value(id, 96) + 96;
    render();
    return;
  }
  if(url.scheme().compare(lit("replaylab-message"), Qt::CaseInsensitive) == 0)
  {
    scrollToMessage(QUrl::fromPercentEncoding(url.path().mid(1).toUtf8()));
    return;
  }
  if(url.scheme().compare(lit("replaylab-artifacts"), Qt::CaseInsensitive) == 0 && // i18n-ignore: transcript action URL scheme.
     url.host().compare(lit("toggle"), Qt::CaseInsensitive) == 0)
  {
    const QString messageId = QUrl::fromPercentEncoding(url.path().mid(1).toUtf8());
    if(QUrlQuery(url).queryItemValue(lit("expanded")) == lit("1")) // i18n-ignore: transcript action URL query.
      m_ExpandedArtifactMessages.insert(messageId);
    else
      m_ExpandedArtifactMessages.remove(messageId);
    render();
    return;
  }
  if(url.scheme().compare(lit("replaylab-activity"), Qt::CaseInsensitive) == 0 &&
     url.host().compare(lit("toggle"), Qt::CaseInsensitive) == 0) // i18n-ignore: transcript action URL host.
  {
    const QString messageId = QUrl::fromPercentEncoding(url.path().mid(1).toUtf8());
    const bool expanded = QUrlQuery(url).queryItemValue(lit("expanded")) == lit("1");
    for(AIChatMessage &message : m_Messages)
    {
      if(message.id == messageId)
      {
        message.activityExpanded = expanded;
        render();
        if(m_ActivityToggleHandler)
          m_ActivityToggleHandler(messageId, expanded);
        return;
      }
    }
    return;
  }

  QUrl target = url;
  if(url.scheme() == QLatin1String("rdoc") && url.host() == QLatin1String("resource"))
  {
    QUrlQuery query(url);
    const QString identity = query.queryItemValue(QLatin1String("visual"));
    if(AIVisualReply::validIdentity(identity))
    {
      query.removeAllQueryItems(QLatin1String("visual"));
      target.setQuery(query.isEmpty() ? QString() : query.toString());
      QJsonObject capture;
      const QUrl scoped = AIVisualReply::resolveLegacyTextureLink(target, identity, capture);
      if(!scoped.isEmpty() && m_VisualLinkHandler)
      {
        m_VisualLinkHandler(scoped, capture);
        return;
      }
    }
  }
  if(m_LinkHandler)
    m_LinkHandler(target);
}

void AIConversationView::render(bool preserveScroll)
{
  if(!m_Browser || !m_TurnRail)
    return;
  m_RenderPendingWhileDetached = false;
  m_RenderPendingForSelection = false;
  if(!preserveScroll)
  {
    m_FollowTail = true;
    clearViewportAnchor();
  }
  else if(!m_FollowTail)
  {
    // Capture the first visible text cursor before setHtml replaces the document. Its text context
    // and viewport offset survive content inserted above the user's reading position.
    captureViewportAnchor();
  }

  m_MessageAnchors.clear();
  m_LiveTextAnchors.clear();
  m_ElapsedAnchors.clear();
  QVector<AITurnRailWidget::Turn> turns;
  QHash<QString, QString> assistantTextByTask;
  for(const AIChatMessage &message : m_Messages)
  {
    // i18n-ignore: Message role is a backend protocol identifier used only for routing.
    if(message.role == lit("assistant") && !message.taskId.isEmpty())
      assistantTextByTask[message.taskId] = message.text;
  }
  for(int i = 0; i < m_Messages.size(); ++i)
  {
    const AIChatMessage &message = m_Messages[i];
    const QString anchor = lit("replaylab-turn-") + QString::number(i);
    m_MessageAnchors[message.id] = anchor;
    if(message.role == lit("user"))
    {
      AITurnRailWidget::Turn turn;
      turn.messageId = message.id;
      turn.userPreview = boundedTurnPreview(message.text, 160);
      if(turn.userPreview.isEmpty())
        turn.userPreview = QCoreApplication::translate("AIConversationView", "Empty user turn");
      if(!message.taskId.isEmpty())
      {
        turn.assistantPreview =
            boundedTurnPreview(assistantTextByTask.value(message.taskId), 360);
        // Live answer text is public assistant output, not private reasoning. Keep failed and
        // interrupted diagnostics out of the navigation preview by using this fallback only while
        // the turn is actively running.
        if(turn.assistantPreview.isEmpty() && isLiveState(message.state))
          turn.assistantPreview = boundedTurnPreview(message.provisionalText, 360);
      }
      else
      {
        // Older stored conversations may predate task IDs. Only for those records, associate the
        // first assistant response before the next user turn.
        for(int responseIndex = i + 1; responseIndex < m_Messages.size(); ++responseIndex)
        {
          const AIChatMessage &response = m_Messages[responseIndex];
          // i18n-ignore: Message roles are backend protocol identifiers used only for routing.
          if(response.role == lit("user"))
            break;
          // i18n-ignore: Message role is a backend protocol identifier used only for routing.
          if(response.role == lit("assistant"))
          {
            turn.assistantPreview = boundedTurnPreview(response.text, 360);
            break;
          }
        }
      }
      turns.push_back(turn);
    }
  }
  m_TurnRail->setTurns(turns);

  const QPalette colors = palette();
  const bool darkTheme = colors.color(QPalette::Base).lightness() <
                         colors.color(QPalette::Text).lightness();
  const QString base = colors.color(QPalette::Base).name();
  const QString alternate = colors.color(QPalette::AlternateBase).name();
  const QString text = colors.color(QPalette::Text).name();
  const QString muted = colors.color(QPalette::Disabled, QPalette::Text).name();
  const QString link = colors.color(QPalette::Link).name();
  const QString border = colors.color(QPalette::Mid).name();
  const QString separator = blendedColor(colors.color(QPalette::Base),
                                         colors.color(QPalette::Text), darkTheme ? 58 : 24)
                                .name();
  const QString highlight = colors.color(QPalette::Highlight).name();
  const QString highlightedText = colors.color(QPalette::HighlightedText).name();
  // Darcula's semantic colours are deliberately kept independent from selection/link colours.
  // The light variants preserve the same token separation without reducing contrast on a bright
  // palette.
  // These are internal CSS colour constants modelled after Rider's Darcula token palette.
  const QString syntaxKeyword = darkTheme ? QLatin1String("#cc7832") : QLatin1String("#0033b3");
  const QString syntaxType = darkTheme ? QLatin1String("#b8d7a3") : QLatin1String("#871094");
  const QString syntaxFunction = darkTheme ? QLatin1String("#ffc66d") : QLatin1String("#00627a");
  const QString syntaxVariable = darkTheme ? QLatin1String("#a9b7c6") : QLatin1String("#202020");
  const QString syntaxString = darkTheme ? QLatin1String("#6a8759") : QLatin1String("#067d17");
  const QString syntaxNumber = darkTheme ? QLatin1String("#6897bb") : QLatin1String("#1750eb");
  const QString syntaxComment = darkTheme ? QLatin1String("#808080") : QLatin1String("#6a737d");
  const QString syntaxParameter = darkTheme ? QLatin1String("#9876aa") : QLatin1String("#9e5b00");

  QHash<QString, qint64> assistantFinishedByTask;
  for(int i = 0; i < m_Messages.size(); ++i)
  {
    const AIChatMessage &message = m_Messages[i];
    // i18n-ignore: Message role is a backend protocol identifier used only for routing.
    if(message.role == lit("assistant") && !message.taskId.isEmpty())
      assistantFinishedByTask[message.taskId] = message.timestampMs;
  }

  // Elapsed labels update whenever lifecycle structure changes. A clock-only refresh must never
  // replace the complete QTextDocument: doing so destroys selections and competes with scroll
  // anchoring even when no conversation text changed.
  const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();

  // Prepare an unobserved document. Replacing the visible document in place causes selection,
  // layout and scrollbar callbacks while the HTML importer is still rebuilding its contents.
  // Attachment resources belong to this document as well, not to the one being replaced.
  QTextDocument *document = new QTextDocument(m_Browser);
  document->setDefaultFont(m_Browser->document()->defaultFont());
  document->setDocumentMargin(m_Browser->document()->documentMargin());
  document->setUndoRedoEnabled(false);

  // i18n-ignore: Structural transcript HTML contains no user-visible prose.
  QString html = lit("<!DOCTYPE html><html><head><style>");
  html +=
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      // The viewport paints the base colour itself (see AIRoundedTextBrowser), so
      // the document body stays transparent and opaque cards can sit behind text.
      lit("body{color:") + text + // i18n-ignore: transcript CSS declaration.
      QLatin1String(";margin:7px;font-family:'Segoe UI Variable Text','Segoe UI','Microsoft YaHei UI',") +
      QLatin1String("'Noto Sans CJK SC',sans-serif;font-size:10pt;line-height:145%;}a{color:") + link +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";}p{margin:3px 0 5px 0;}ul,ol{margin-top:4px;margin-bottom:6px;") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit("padding-left:22px;}li{margin:2px 0;}h3,h4,h5,h6{margin:7px 0 3px 0;}") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit("pre{white-space:pre-wrap;background:") + alternate + lit(";border:1px solid ") + border +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";margin:6px 0;padding:8px;font-family:'Cascadia Code','Cascadia Mono',Consolas,") +
      QLatin1String("'SFMono-Regular',monospace;}code{font-family:'Cascadia Code','Cascadia Mono',Consolas,") +
      QLatin1String("'SFMono-Regular',monospace;background:") + alternate +
      QLatin1String(";}.source-block{font-family:'Cascadia Code','Cascadia Mono',Consolas,") +
      QLatin1String("'SFMono-Regular',monospace;font-size:9.5pt;line-height:125%;background:") + alternate +
      QLatin1String(";border:1px solid ") + border + QLatin1String(";margin:7px 0 9px 0;") +
      QLatin1String("}.source-header{font-family:'Segoe UI Variable Text','Segoe UI','Microsoft YaHei UI',") +
      QLatin1String("sans-serif;color:") + muted +
      QLatin1String(";font-size:9pt;font-weight:600;padding:5px 8px;") +
      QLatin1String("border-bottom:1px solid ") + border +
      QLatin1String(";}.source-line-number{color:") + muted +
      QLatin1String(";padding:0 8px 0 4px;border-right:1px solid ") + border +
      QLatin1String(";}.source-line{color:") + text +
      QLatin1String(";padding:0 8px;white-space:pre-wrap;") +
      QLatin1String("}.source-keyword{color:") + syntaxKeyword +
      QLatin1String(";font-weight:600;}.source-type{color:") + syntaxType +
      QLatin1String(";}.source-function{color:") + syntaxFunction +
      QLatin1String(";}.source-variable{color:") + syntaxVariable +
      QLatin1String(";}.source-key{color:") + syntaxParameter +
      QLatin1String(";font-weight:600;}.source-string{color:") + syntaxString +
      QLatin1String(";}.source-comment{color:") + syntaxComment +
      QLatin1String(";font-style:italic;}.source-number{color:") + syntaxNumber +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";}.bubble-marker,.message-marker{font-size:1px;}.user-bubble{padding:7px 10px;}.assistant{") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit("margin:7px 4px 6px 4px;") + lit("padding:0;background:") + base + lit(";}.system{color:") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      muted + lit(";margin:3px 4px;padding-left:7px;border-left:2px solid ") + border +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";}.meta{color:") + muted +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";font-size:small;margin-bottom:3px;}.activity-group{margin:5px 4px 10px 4px;") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit("}.activity-header{color:") + muted +
      QLatin1String(";font-size:small;}.activity-disclosure{color:") + text +
      QLatin1String(";font-family:'Segoe UI Symbol','Noto Sans Symbols','Arial Unicode MS',sans-serif;") +
      QLatin1String("font-size:16px;line-height:100%;text-decoration:none;}.activity-toggle{color:") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      muted + lit(";text-decoration:none;}hr{border:0;border-top:1px solid ") + separator +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";background-color:") + separator + lit(";color:") + separator +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";height:1px;margin:4px 0 7px 0;}") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      // The card panel (dark, rounded) is painted by the browser; the table only
      // spaces its rows and colours the text for that dark panel.
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(".artifact-card{margin:12px 0 8px 0;}.artifact-head{color:#f2f2f2;font-weight:600;") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit("font-size:12px;padding:8px 12px 6px 12px;}.artifact-row{padding:3px 12px;font-size:12px;}") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(".artifact-foot{padding:0 12px 4px 12px;font-size:4px;}") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(".artifact-badge{padding:3px 12px;font-size:11px;color:#9a9a9a;}") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(".artifact-link{color:#e6e6e6;text-decoration:none;}.artifact-add{color:#3fb950;}") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(".artifact-toggle{color:#9a9a9a;text-decoration:none;}.artifact-more{padding:4px 12px 6px 12px;}") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(".artifact-del{color:#f85149;}.artifact-child{color:#b0b0b0;") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit("}.artifact-failed{color:#8a8a8a;") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";text-decoration:line-through;}.activity-list{margin-left:9px;padding-left:11px;") +
      QLatin1String("border-left:1px solid ") + border +
      QLatin1String(";}.activity-row{margin:4px 0 9px 0;color:") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      text + lit(";}.activity-marker{font-size:13px;}.activity-title{color:") + text +
      QLatin1String(";font-weight:600;margin:0 0 2px 0;}.activity-meta{margin:0 0 3px 0;") +
      QLatin1String("line-height:130%;}.activity-kind{color:") + muted +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";font-size:small;}.activity-state{color:") + muted +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";font-size:small;}.activity-detail{color:") + muted +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";margin:3px 0 0 0;line-height:145%;}.activity-live-text{white-space:pre-wrap;") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit("}.activity-live-tail{color:") + muted +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";text-decoration:none;}.activity-code{font-family:'Cascadia Code',") +
      QLatin1String("'Cascadia Mono',Consolas,'SFMono-Regular',monospace;font-size:9pt;") +
      QLatin1String("line-height:125%;background:") + alternate +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";border:1px solid ") + border + lit(";margin:4px 0 2px 0;padding:2px;") +
      QLatin1String("}.code-line-number{color:") + muted +
      QLatin1String(";padding:0 7px 0 3px;border-right:1px solid ") + border +
      QLatin1String(";}.code-line{color:") + text +
      QLatin1String(";padding:0 5px;white-space:pre-wrap;") +
      QLatin1String("}.code-omitted{color:") + muted +
      QLatin1String(";font-style:italic;padding:3px 5px;") +
      QLatin1String("}.detail-key{color:") + syntaxParameter +
      QLatin1String(";font-weight:600;}.detail-string{color:") + syntaxString +
      QLatin1String(";}.detail-number{color:") + syntaxNumber +
      QLatin1String(";}.detail-literal{color:") + syntaxKeyword +
      QLatin1String(";}.detail-type{color:") + syntaxType +
      QLatin1String(";}.detail-function{color:") + syntaxFunction +
      QLatin1String(";}.detail-variable{color:") + syntaxVariable +
      QLatin1String(";}.detail-comment{color:") + syntaxComment +
      QLatin1String(";font-style:italic") +
      QLatin1String(";}.detail-status{color:") + highlight +
      QLatin1String(";font-weight:600;") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit("}.activity-running{color:") + highlight +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";}.activity-completed{color:") + highlight + lit(";}.activity-error{color:") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      highlightedText + lit(";background:") + highlight + lit(";}.live{margin:4px 4px 6px 4px;") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit("padding-left:8px;border-left:2px solid ") + highlight +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";}.message-attachments{margin:1px 0 6px 0;}.message-attachment{") +
      lit("display:inline-block;border:1px solid ") + border + lit(";background:") + alternate +    // i18n-ignore: transcript CSS declarations
      lit(";border-radius:12px;padding:2px 7px;margin:0 5px 3px 0;color:") + muted +    // i18n-ignore: transcript CSS declarations
      lit(";}blockquote{border-left:3px solid ") + border +    // i18n-ignore: transcript CSS declarations
      // i18n-ignore: Transcript CSS declarations and closing HTML contain no user-visible text.
      lit(";padding-left:8px;margin:2px 0 2px 4px;}</style></head><body>");

  QVector<QPair<QString, QString>> bubbleAnchors;
  QVector<QPair<QString, QString>> cardAnchors;
  for(int messageIndex = 0; messageIndex < m_Messages.size(); ++messageIndex)
  {
    const AIChatMessage &message = m_Messages[messageIndex];
    const QStringList visualIdentities = AIVisualReply::visibleIdentities(message.visuals);
    const QString anchor = m_MessageAnchors.value(message.id);
    // i18n-ignore: Message role is a backend protocol identifier used only for routing.
    if(message.role == lit("user"))
    {
      // i18n-ignore: Generated anchor prefix is an internal transcript navigation identifier.
      const QString bubbleStart = lit("replaylab-bubble-start-") + QString::number(messageIndex);
      // i18n-ignore: Generated anchor prefix is an internal transcript navigation identifier.
      const QString bubbleEnd = lit("replaylab-bubble-end-") + QString::number(messageIndex);
      bubbleAnchors.push_back(qMakePair(bubbleStart, bubbleEnd));
      // i18n-ignore: Structural transcript HTML contains no user-visible prose.
      html += lit("<table width=\"100%\" border=\"0\" cellspacing=\"0\" cellpadding=\"0\">") +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              lit("<tr><td width=\"29%\"></td><td width=\"71%\" class=\"user-bubble\">") +
              // i18n-ignore: Structural transcript HTML and zero-width marker contain no prose.
              lit("<a class=\"message-marker\" name=\"") + anchor.toHtmlEscaped() +
              // i18n-ignore: Structural transcript HTML and zero-width marker contain no prose.
              lit("\">&#8203;</a>") +
              // i18n-ignore: Structural transcript HTML and zero-width marker contain no prose.
              lit("<a class=\"bubble-marker\" name=\"") + bubbleStart +
              // i18n-ignore: Structural transcript HTML and zero-width marker contain no prose.
              lit("\">&#8203;</a>");
      // i18n-ignore: Conversation state is an internal lifecycle identifier used only for control flow.
      if(!message.state.isEmpty() && message.state != lit("completed"))
        // i18n-ignore: Structural transcript HTML contains no user-visible prose.
        html += lit("<div class=\"meta\">") + messageStateLabel(message.state).toHtmlEscaped() +
                // i18n-ignore: Structural transcript HTML contains no user-visible prose.
                lit("</div>");
      // i18n-ignore: Structural transcript HTML contains no user-visible prose.
      html += renderMessageAttachments(message.attachments, document) +
              safeMarkdown(message.text) +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              lit("<a class=\"bubble-marker\" name=\"") + bubbleEnd +
              // i18n-ignore: Structural transcript HTML and zero-width marker contain no prose.
              lit("\">&#8203;</a></td></tr></table>");

      if(!message.activity.isEmpty())
      {
        const qint64 finishedMs = message.finishedTimestampMs > 0
                                      ? message.finishedTimestampMs
                                      : assistantFinishedByTask.value(message.taskId, 0);
        html += renderActivityGroup(message, finishedMs, nowMs, &m_LiveTextAnchors,
                                    &m_ElapsedAnchors, m_ActivityHistoryLimits.value(message.id, 96));
      }
    }
    // i18n-ignore: Message role is a backend protocol identifier used only for routing.
    else if(message.role == lit("assistant"))
    {
      QString formattedText = safeMarkdown(message.text);
      if(!visualIdentities.isEmpty())
      {
        // Older resource links did not encode the EID. Carry the visual identity
        // so the click can resolve the event from its verified diagram.
        static const QRegularExpression resourceHref(
            QStringLiteral(R"link(href="(rdoc://resource/(?:ResourceId::)?[0-9]+)")link"));
        formattedText.replace(
            resourceHref, QStringLiteral("href=\"\\1?visual=%1\"").arg(visualIdentities.last()));
      }
      if(!message.artifacts.isEmpty())
        // i18n-ignore: Generated anchor prefixes are internal transcript identifiers.
        cardAnchors.push_back(qMakePair(lit("replaylab-card-start-") + QString::number(messageIndex),
                                        lit("replaylab-card-end-") + QString::number(messageIndex))); // i18n-ignore: generated transcript anchor identifier.
      // i18n-ignore: Structural transcript HTML and zero-width marker contain no prose.
      html += lit("<div class=\"assistant\"><a class=\"message-marker\" name=\"") +
              anchor.toHtmlEscaped() +
              // i18n-ignore: Structural transcript HTML and zero-width marker contain no prose.
              lit("\">&#8203;</a>") + renderMessageAttachments(message.attachments, document) +
              formattedText +
              renderMessageArtifacts(message, m_ExpandedArtifactMessages.contains(message.id),
                                     messageIndex) +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              lit("</div>");
    }
    else
    {
      // i18n-ignore: Structural transcript HTML and zero-width marker contain no prose.
      html += lit("<div class=\"system\"><a class=\"message-marker\" name=\"") +
              anchor.toHtmlEscaped() +
              // i18n-ignore: Structural transcript HTML and zero-width marker contain no prose.
              lit("\">&#8203;</a>") +
              renderMessageAttachments(message.attachments, document) +
              safeMarkdown(message.text) +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              lit("</div>");
    }

    // i18n-ignore: Message roles are backend protocol identifiers used only for routing.
    if(!message.activity.isEmpty() && message.role != lit("user") && message.role != lit("assistant"))
    {
      const qint64 finishedMs = message.finishedTimestampMs > 0
                                    ? message.finishedTimestampMs
                                    : assistantFinishedByTask.value(message.taskId, 0);
      html += renderActivityGroup(message, finishedMs, nowMs, &m_LiveTextAnchors,
                                  &m_ElapsedAnchors, m_ActivityHistoryLimits.value(message.id, 96));
    }

    if(!message.provisionalText.isEmpty())
    {
      QString provisionalLabel =
          QCoreApplication::translate("AIConversationView", "AI (live)");
      if(message.state == lit("superseded") || message.state == lit("interrupted") ||
         message.state == lit("cancelled"))
        provisionalLabel =
            QCoreApplication::translate("AIConversationView", "Draft before interruption");
      else if(message.state == lit("failed"))
        provisionalLabel = QCoreApplication::translate("AIConversationView", "Failure details");
      // i18n-ignore: Structural transcript HTML contains no user-visible prose.
      html += lit("<div class=\"live\"><div class=\"meta\">") + provisionalLabel.toHtmlEscaped() +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              lit("</div>") + safeMarkdown(message.provisionalText) + lit("</div>");
    }
    for(const QString &identity : visualIdentities)
    {
      const QString slot =
          QLatin1String("bread-visual-slot:") + message.id + QLatin1Char(':') + identity;
      QImage transparent(1, 1, QImage::Format_ARGB32);
      transparent.fill(Qt::transparent);
      document->addResource(QTextDocument::ImageResource, QUrl(slot), transparent);
      // i18n-ignore: Native visual placeholder HTML contains only a verified identity and dimensions.
      html += QStringLiteral("<p><img src=\"%1\" width=\"%2\" height=\"%3\" /></p>")
                  .arg(slot.toHtmlEscaped())
                  .arg(qMax(180, m_Browser->viewport()->width() - 28))
                  .arg(AIVisualReply::inlineHeight(identity));
    }
  }
  html += lit("</body></html>");

  const int generation = ++m_RenderGeneration;
  const quint64 userInteractionGeneration = m_UserInteractionGeneration;
  document->setHtml(inlineTranscriptStyles(html));
  m_AdjustingScroll = true;
  m_LiveTextCursors.clear();
  m_ElapsedCursors.clear();
  static_cast<AIRoundedTextBrowser *>(m_Browser)->setBubbleAnchors({});
  static_cast<AIRoundedTextBrowser *>(m_Browser)->setCardAnchors({});
  static_cast<AIRoundedTextBrowser *>(m_Browser)->setElapsedOverlays({});
  // QTextEdit deletes its original control-owned document, but not externally supplied ones.
  // QPointer covers both cases; do not accumulate a document for every streamed update.
  QPointer<QTextDocument> previousDocument = m_Browser->document();
  m_Browser->setDocument(document);
  delete previousDocument.data();
  m_VisualPositions.clear();
  for(QTextBlock block = document->begin(); block.isValid(); block = block.next())
    for(QTextBlock::iterator it = block.begin(); !it.atEnd(); ++it)
    {
      const QTextFragment fragment = it.fragment();
      if(!fragment.charFormat().isImageFormat())
        continue;
      const QString name = fragment.charFormat().toImageFormat().name();
      if(!name.startsWith(QLatin1String("bread-visual-slot:")))
        continue;
      QTextCursor cursor(document);
      cursor.setPosition(fragment.position());
      cursor.setPosition(fragment.position() + 1, QTextCursor::KeepAnchor);
      m_VisualPositions.insert(name.mid(18), cursor);
    }
  rebuildLiveTextCursors();
  rebuildElapsedCursors();
  updateLiveTextTimer();
  static_cast<AIRoundedTextBrowser *>(m_Browser)->setBubbleAnchors(bubbleAnchors);
  static_cast<AIRoundedTextBrowser *>(m_Browser)->setCardAnchors(cardAnchors);
  QTimer::singleShot(0, this, [this, generation, userInteractionGeneration]() {
    if(generation != m_RenderGeneration)
      return;
    if(userInteractionGeneration != m_UserInteractionGeneration)
    {
      m_AdjustingScroll = false;
      updateTailControl();
      return;
    }
    if(m_FollowTail)
      scrollToLatest();
    else
      restoreViewportAnchor();
    // Keep lazy QTextDocument range changes inside the same protected layout turn. A later render
    // owns its own generation and will release this flag from its corresponding callback.
    m_AdjustingScroll = false;
    updateTailControl();
    updateVisualReplies();
  });
}
