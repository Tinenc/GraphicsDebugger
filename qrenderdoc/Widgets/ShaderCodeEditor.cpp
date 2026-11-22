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

#include "ShaderCodeEditor.h"
#include <climits>

#include <QAction>
#include <QApplication>
#include <QClipboard>
#include <QCursor>
#include <QInputDialog>
#include <QHBoxLayout>
#include <QKeyEvent>
#include <QMenu>
#include <QMimeData>
#include <QMouseEvent>
#include <QToolTip>
#include <QToolButton>
#include <QStyle>
#include <QTimer>
#include <QVBoxLayout>

#include "Code/CodeEditorService.h"
#include "Code/QRDUtils.h"
#include "Code/ScintillaSyntax.h"
#include "Widgets/FindReplace.h"
#include "scintilla/include/Scintilla.h"
#include "scintilla/include/qt/ScintillaEdit.h"

namespace
{
const int MinimumLineNumberDigits = 3;
const int SourceAnchorMarker = 23;
const int VisualDebugLineMarker = 24;
const int VisualDebugExpressionIndicator = 14;
}

ShaderCodeEditor::ShaderCodeEditor(CodeEditorService *services, QWidget *parent)
    : QWidget(parent), m_Services(services)
{
  QVBoxLayout *layout = new QVBoxLayout(this);
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(0);

  QHBoxLayout *navigation = new QHBoxLayout();
  navigation->setContentsMargins(0, 0, 0, 0);
  QToolButton *back = new QToolButton(this);
  back->setObjectName(QStringLiteral("shaderCodeBack")); // i18n-ignore: automation id.
  back->setIcon(style()->standardIcon(QStyle::SP_ArrowBack));
  back->setToolTip(tr("Navigate Back (Alt+Left)"));
  back->setAutoRaise(true);
  QToolButton *forward = new QToolButton(this);
  forward->setObjectName(QStringLiteral("shaderCodeForward")); // i18n-ignore: automation id.
  forward->setIcon(style()->standardIcon(QStyle::SP_ArrowForward));
  forward->setToolTip(tr("Navigate Forward (Alt+Right)"));
  forward->setAutoRaise(true);
  navigation->addWidget(back);
  navigation->addWidget(forward);
  QToolButton *symbols = new QToolButton(this);
  symbols->setObjectName(QStringLiteral("shaderCodeSymbols")); // i18n-ignore: automation id.
  symbols->setText(tr("Symbols"));
  symbols->setToolTip(tr("Browse symbols in this source revision"));
  symbols->setAutoRaise(true);
  navigation->addWidget(symbols);
  navigation->addStretch(1);
  layout->addLayout(navigation);
  QObject::connect(symbols, &QToolButton::clicked, this, [this, symbols]() {
    if(!m_Services) return;
    const QList<CodeEditorService::SymbolInfo> table = m_Services->symbolTable(m_Editor);
    const QString sourceSha256 = m_Services->documentSourceSha256(m_Editor);
    QMenu *menu = new QMenu(this);
    menu->setAttribute(Qt::WA_DeleteOnClose);
    menu->addSection(tr("Global symbols"));
    int shown = 0;
    for(const CodeEditorService::SymbolInfo &symbol : table)
    {
      if(!symbol.global || shown >= 128) continue;
      const QString label = tr("%1 (line %2)").arg(symbol.name).arg(symbol.declaration.line + 1);
      QAction *action = menu->addAction(label);
      QObject::connect(action, &QAction::triggered, this, [this, sourceSha256, symbol]() {
        if(m_Services && m_Services->documentSourceSha256(m_Editor) == sourceSha256)
          GoToPosition(symbol.declaration.position);
        else
          SetStatusMessage(tr("Symbol index changed; open the list again."));
      });
      shown++;
    }
    if(shown == 0)
      menu->addAction(tr("No global symbols found"))->setEnabled(false);
    else if(shown >= 128)
      menu->addAction(tr("Showing the first 128 symbols"))->setEnabled(false);
    menu->popup(symbols->mapToGlobal(QPoint(0, symbols->height())));
  });
  QObject::connect(back, &QToolButton::clicked, this, [this]() {
    if(m_Services) m_Services->navigateHistory(m_Editor, true);
  });
  QObject::connect(forward, &QToolButton::clicked, this, [this]() {
    if(m_Services) m_Services->navigateHistory(m_Editor, false);
  });
  auto updateNavigation = [this, back, forward]() {
    back->setEnabled(m_Services && m_Services->canNavigateHistory(true));
    forward->setEnabled(m_Services && m_Services->canNavigateHistory(false));
  };
  QTimer *navigationTimer = new QTimer(this);
  navigationTimer->setInterval(200);
  QObject::connect(navigationTimer, &QTimer::timeout, this, updateNavigation);
  navigationTimer->start();
  updateNavigation();

  m_FindReplace = new FindReplace(this);
  m_FindReplace->allowUserModeChange(false);
  m_FindReplace->hide();
  layout->addWidget(m_FindReplace);

  m_Editor = new ScintillaEdit(this);
  m_Editor->setObjectName(QStringLiteral("shaderCodeEditor"));    // i18n-ignore: automation id.
  m_Editor->setMarginLeft(4.0);
  m_Editor->setMarginTypeN(0, SC_MARGIN_NUMBER);
  m_Editor->markerDefine(SourceAnchorMarker, SC_MARK_EMPTY);
  m_Editor->markerDefine(VisualDebugLineMarker, SC_MARK_BACKGROUND);
  m_Editor->markerSetBack(VisualDebugLineMarker, 0x2080e0);
  m_Editor->markerSetAlpha(VisualDebugLineMarker, 45);
  m_Editor->indicSetStyle(VisualDebugExpressionIndicator, INDIC_ROUNDBOX);
  m_Editor->indicSetFore(VisualDebugExpressionIndicator, 0x2080e0);
  m_Editor->indicSetAlpha(VisualDebugExpressionIndicator, 80);
  m_Editor->indicSetOutlineAlpha(VisualDebugExpressionIndicator, 230);
  m_Editor->indicSetUnder(VisualDebugExpressionIndicator, true);
  m_Editor->setMarginWidthN(1, 0);
  m_Editor->setMarginWidthN(2, 0);
  m_Editor->styleSetFont(STYLE_DEFAULT, Formatter::FixedFont().family().toUtf8().constData());
  m_Editor->styleSetSize(STYLE_DEFAULT, Formatter::FixedFont().pointSize());
  m_Editor->setCodePage(SC_CP_UTF8);
  m_Editor->setTabWidth(4);
  m_Editor->setUseTabs(false);
  m_Editor->setWrapMode(SC_WRAP_NONE);
  m_Editor->setScrollWidth(1);
  m_Editor->setScrollWidthTracking(true);
  m_Editor->setCaretLineVisible(true);
  m_Editor->setCaretLineBackAlpha(32);
  m_Editor->setMultipleSelection(true);
  m_Editor->setAdditionalSelectionTyping(true);
  m_Editor->setMultiPaste(SC_MULTIPASTE_EACH);
  m_Editor->setMouseDwellTime(500);
  m_Editor->setContextMenuPolicy(Qt::CustomContextMenu);
  ConfigureSyntax(m_Editor, SCLEX_HLSL);
  UpdateLineNumberMargin();
  layout->addWidget(m_Editor, 1);
  m_DiagnosticSummary = new QToolButton(this);
  m_DiagnosticSummary->setObjectName(QStringLiteral("shaderCodeDiagnostics")); // i18n-ignore: automation id.
  m_DiagnosticSummary->setAutoRaise(true);
  m_DiagnosticSummary->hide();
  layout->addWidget(m_DiagnosticSummary);
  QObject::connect(m_DiagnosticSummary, &QToolButton::clicked, this,
                   [this]() { GoToLine(uint32_t(m_FirstDiagnosticLine)); });

  if(m_Services)
    m_Services->registerEditor(m_Editor, m_FileName, m_Language);

  m_Editor->installEventFilter(this);
  QObject::connect(m_Editor, &ScintillaEdit::keyPressed, this,
                   [this](QKeyEvent *event) { HandleKeyPress(event); });
  QObject::connect(m_Editor, &ScintillaEdit::modified, this,
                   [this](int type, int position, int length, int, const QByteArray &text, int, int, int) {
                     if(type & (SC_MOD_INSERTTEXT | SC_MOD_DELETETEXT))
                     {
                       if(!m_SettingText)
                         TrackRangeEdit(type, position, length, text);
                       SetVisualDebugRange(0, 0, false);
                       UpdateLineNumberMargin();
                       if(!m_SettingText && m_TextChangedCallback)
                         m_TextChangedCallback();
                     }
                   });
  QObject::connect(QApplication::clipboard(), &QClipboard::dataChanged, this, [this]() {
    if(m_ClipboardAction != 1)
    {
      m_CutRanges.clear();
      m_CutText.clear();
    }
  });
  QObject::connect(m_Editor, &QWidget::customContextMenuRequested, this,
                   &ShaderCodeEditor::ShowContextMenu);

  m_AddToChatButton = new QToolButton(this);
  m_AddToChatButton->setObjectName(QStringLiteral("shaderCodeAddToChat"));    // i18n-ignore: automation id.
  m_AddToChatButton->setText(tr("Add to chat"));
  m_AddToChatButton->setToolButtonStyle(Qt::ToolButtonTextOnly);
  m_AddToChatButton->setFocusPolicy(Qt::NoFocus);
  m_AddToChatButton->setCursor(Qt::PointingHandCursor);
  m_AddToChatButton->setAttribute(Qt::WA_ShowWithoutActivating);
  // i18n-ignore: Qt stylesheet implementation; the visible button text is translated above.
  m_AddToChatButton->setStyleSheet(lit(
      "QToolButton { background: palette(base); color: palette(text); "
      "border: 1px solid palette(mid); border-radius: 13px; padding: 4px 12px; } "
      "QToolButton:hover { background: palette(midlight); }"));
  m_AddToChatButton->hide();
  QObject::connect(m_AddToChatButton, &QToolButton::clicked, this, [this]() {
    m_AddToChatButton->hide();
    AddSelectionToChat();
  });
  m_VisualDebugButton = new QToolButton(this);
  m_VisualDebugButton->setObjectName(QStringLiteral("shaderCodeVisualDebug")); // i18n-ignore: automation id.
  m_VisualDebugButton->setText(tr("Output to RT"));
  m_VisualDebugButton->setToolTip(tr("Output this value to a render target (Ctrl+E)"));
  m_VisualDebugButton->setToolButtonStyle(Qt::ToolButtonTextOnly);
  m_VisualDebugButton->setFocusPolicy(Qt::NoFocus);
  m_VisualDebugButton->setCursor(Qt::PointingHandCursor);
  m_VisualDebugButton->setAttribute(Qt::WA_ShowWithoutActivating);
  m_VisualDebugButton->setStyleSheet(m_AddToChatButton->styleSheet());
  m_VisualDebugButton->hide();
  QObject::connect(m_VisualDebugButton, &QToolButton::clicked, this,
                   &ShaderCodeEditor::RequestVisualDebug);
  QObject::connect(m_Editor, &ScintillaEdit::buttonPressed, this, [this](QMouseEvent *event) {
    if(event->button() == Qt::LeftButton)
    {
      m_SelectingWithMouse = true;
      m_AddToChatButton->hide();
      m_VisualDebugButton->hide();
    }
  });
  QObject::connect(m_Editor, &ScintillaEdit::buttonReleased, this, [this](QMouseEvent *event) {
    if(event->button() == Qt::LeftButton)
    {
      m_SelectingWithMouse = false;
      UpdateAddToChatButton();
      UpdateVisualDebugButton();
    }
  });
  QObject::connect(m_Editor, &ScintillaEdit::updateUi, this,
                   [this]() { UpdateAddToChatButton(); UpdateVisualDebugButton(); });
  QObject::connect(m_Editor, &ScintillaEdit::horizontalScrolled, this,
                   [this](int) { PositionAddToChatButton(); UpdateVisualDebugButton(); });
  QObject::connect(m_Editor, &ScintillaEdit::verticalScrolled, this,
                   [this](int) { PositionAddToChatButton(); UpdateVisualDebugButton(); });

  QObject::connect(m_FindReplace, &FindReplace::performFindRequested, this,
                   [this]() { FindNext(m_FindReplace->direction() == FindReplace::Down); });
  QObject::connect(m_FindReplace, &FindReplace::performFindAllRequested, this,
                   &ShaderCodeEditor::UpdateFindCount);
  QObject::connect(m_FindReplace, &FindReplace::performReplaceRequested, this,
                   &ShaderCodeEditor::ReplaceCurrent);
  QObject::connect(m_FindReplace, &FindReplace::performReplaceAllRequested, this,
                   &ShaderCodeEditor::ReplaceAll);
  QObject::connect(m_FindReplace, &FindReplace::searchChanged, this,
                   &ShaderCodeEditor::FindIncremental);
  QObject::connect(m_FindReplace, &FindReplace::closeRequested, this,
                   &ShaderCodeEditor::CloseFind);
  QObject::connect(m_FindReplace, &FindReplace::keyPress, this,
                   [this](QKeyEvent *event) { HandleKeyPress(event); });
}

ShaderCodeEditor::~ShaderCodeEditor()
{
  ClearCallbacks();
  if(m_Services && m_Editor)
    m_Services->unregisterEditor(m_Editor);
}

rdcstr ShaderCodeEditor::GetText()
{
  return QString::fromUtf8(m_Editor->getText(m_Editor->textLength() + 1));
}

void ShaderCodeEditor::SetText(const rdcstr &text)
{
  const bool readOnly = m_Editor->readOnly();
  m_SettingText = true;
  ClearLineAnchors();
  m_RangeAnchors.clear();
  m_RangeHistory.clear();
  m_RangeHistoryCursor = 0;
  m_ReplacementRanges.clear();
  m_CutRanges.clear();
  m_CutText.clear();
  if(m_Services) m_Services->clearNavigation(m_Editor);
  if(readOnly)
    m_Editor->setReadOnly(false);
  const QByteArray bytes = QString(text).toUtf8();
  const int newline = bytes.indexOf('\n');
  // New lines must use the loaded file's convention. Scintilla's Windows default is
  // CRLF even for an LF document, which otherwise silently introduces mixed endings.
  m_Editor->setEOLMode(newline >= 0 ? (newline > 0 && bytes[newline - 1] == '\r'
                                        ? SC_EOL_CRLF : SC_EOL_LF)
                                  : (bytes.contains('\r') ? SC_EOL_CR : SC_EOL_LF));
  m_Editor->setText(bytes.constData());
  m_Editor->emptyUndoBuffer();
  if(readOnly)
    m_Editor->setReadOnly(true);
  m_SettingText = false;
  UpdateLineNumberMargin();
}

void ShaderCodeEditor::SetReadOnly(bool readOnly)
{
  m_Editor->setReadOnly(readOnly);
}

void ShaderCodeEditor::SetNavigationEnabled(bool enabled)
{
  if(m_NavigationEnabled == enabled) return;
  m_NavigationEnabled = enabled;
  if(!m_Services) return;
  if(enabled) RegisterDocument();
  else m_Services->unregisterEditor(m_Editor);
}

bool ShaderCodeEditor::IsReadOnly()
{
  return m_Editor->readOnly();
}

void ShaderCodeEditor::SetFileName(const rdcstr &fileName)
{
  const QString value = QString(fileName);
  if(m_FileName == value)
    return;
  m_FileName = value;
  RegisterDocument();
}

void ShaderCodeEditor::SetIncludeBaseDirectory(const rdcstr &directory)
{
  const QString value = QString(directory);
  if(m_IncludeBaseDirectory == value)
    return;
  m_IncludeBaseDirectory = value;
  RegisterDocument();
}

void ShaderCodeEditor::SetLanguage(const rdcstr &language)
{
  QString value = QString(language).trimmed().toLower();
  if(value.isEmpty())
    value = QStringLiteral("hlsl");    // i18n-ignore: LSP language identifier.
  if(m_Language == value)
    return;
  m_Language = value;
  ConfigureSyntax(m_Editor, value == QStringLiteral("glsl") ? SCLEX_GLSL : SCLEX_HLSL);
  RegisterDocument();
}

bool ShaderCodeEditor::IsModified()
{
  return m_ForcedModified || m_Editor->modify();
}

void ShaderCodeEditor::SetModified(bool modified)
{
  if(!modified)
  {
    SetSavePoint();
    return;
  }
  m_ForcedModified = true;
}

void ShaderCodeEditor::SetSavePoint()
{
  m_ForcedModified = false;
  m_Editor->setSavePoint();
}

void ShaderCodeEditor::GoToLine(uint32_t line, uint32_t column)
{
  const int zeroBasedLine = qBound(0, int(line) - 1, qMax(0, int(m_Editor->lineCount()) - 1));
  const int lineStart = int(m_Editor->positionFromLine(zeroBasedLine));
  const int lineEnd = int(m_Editor->lineEndPosition(zeroBasedLine));
  const int position = qBound(
      lineStart, int(m_Editor->findColumn(zeroBasedLine, qMax(0, int(column) - 1))), lineEnd);
  if(m_Services)
    m_Services->rememberNavigation(m_Editor, position);
  GoToPosition(position);
}

void ShaderCodeEditor::SelectRange(uint32_t byteStart, uint32_t byteEnd)
{
  const uint32_t length = uint32_t(m_Editor->length());
  const int start = int(qMin(byteStart, length));
  const int end = int(qMax(uint32_t(start), qMin(byteEnd, length)));
  if(m_Services)
    m_Services->rememberNavigation(m_Editor, start);
  GoToPosition(start);
  m_Editor->setSel(start, end);
  m_Editor->scrollCaret();
}

int32_t ShaderCodeEditor::CreateLineAnchor(uint32_t line)
{
  if(line == 0 || line > uint32_t(m_Editor->lineCount()))
    return -1;
  const int anchor = int(m_Editor->markerAdd(int(line) - 1, SourceAnchorMarker));
  if(anchor >= 0)
  {
    m_LineAnchors.insert(anchor);
    const int start = int(m_Editor->positionFromLine(int(line) - 1));
    const int end = int(m_Editor->lineEndPosition(int(line) - 1));
    int token = start;
    while(token < end && (m_Editor->charAt(token) == ' ' || m_Editor->charAt(token) == '\t'))
      token++;
    const int stop = token < end ? end :
                                  qMin(start + 1, int(m_Editor->length()));
    const int range = CreateRangeAnchor(uint32_t(token < end ? token : start), uint32_t(stop));
    if(range >= 0)
      m_LineAnchorRanges[anchor] = range;
  }
  return anchor;
}

uint32_t ShaderCodeEditor::GetLineAnchorLine(int32_t anchor)
{
  if(!m_LineAnchors.contains(anchor))
    return 0;
  if(m_LineAnchorRanges.contains(anchor))
  {
    rdcarray<uint32_t> range = GetRangeAnchorRange(m_LineAnchorRanges[anchor]);
    return range.empty() ? 0 : uint32_t(m_Editor->lineFromPosition(range[0]) + 1);
  }
  const int line = int(m_Editor->markerLineFromHandle(anchor));
  return line >= 0 ? uint32_t(line + 1) : 0;
}

void ShaderCodeEditor::RemoveLineAnchor(int32_t anchor)
{
  if(m_LineAnchors.remove(anchor))
  {
    m_Editor->markerDeleteHandle(anchor);
    if(m_LineAnchorRanges.contains(anchor))
      RemoveRangeAnchor(m_LineAnchorRanges.take(anchor));
  }
}

void ShaderCodeEditor::ClearLineAnchors()
{
  for(int anchor : m_LineAnchors)
  {
    m_Editor->markerDeleteHandle(anchor);
    if(m_LineAnchorRanges.contains(anchor))
      RemoveRangeAnchor(m_LineAnchorRanges.take(anchor));
  }
  m_LineAnchors.clear();
}

int32_t ShaderCodeEditor::CreateRangeAnchor(uint32_t byteStart, uint32_t byteEnd)
{
  if(byteStart >= byteEnd || byteEnd > uint32_t(m_Editor->length()) ||
     m_NextRangeAnchor == INT_MAX)
    return -1;
  const int id = m_NextRangeAnchor++;
  RangeAnchor anchor;
  anchor.start = int(byteStart);
  anchor.end = int(byteEnd);
  anchor.valid = true;
  m_RangeAnchors[id] = anchor;
  // Newly created anchors also belong to the current undo boundary. They are
  // not retroactively attached to unrelated text before their creation.
  if(m_RangeHistoryCursor > 0 && m_RangeHistoryCursor <= m_RangeHistory.size())
    m_RangeHistory[m_RangeHistoryCursor - 1].after[id] = anchor;
  if(m_RangeHistoryCursor >= 0 && m_RangeHistoryCursor < m_RangeHistory.size())
    m_RangeHistory[m_RangeHistoryCursor].before[id] = anchor;
  return id;
}

rdcarray<uint32_t> ShaderCodeEditor::GetRangeAnchorRange(int32_t id)
{
  rdcarray<uint32_t> result;
  auto it = m_RangeAnchors.constFind(id);
  if(it == m_RangeAnchors.constEnd() || !it->valid || it->start >= it->end ||
     it->end > m_Editor->length())
    return result;
  int start = it->start, end = it->end;
  auto word = [](int c) { return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                                (c >= '0' && c <= '9') || c == '_'; };
  // Endpoint affinity follows the original bytes. Expanding only the current
  // lexical token also handles prefix/suffix typing without following an
  // unrelated statement that the user inserts immediately before the range.
  if(start > 0 && word(int(m_Editor->charAt(start - 1))) && word(int(m_Editor->charAt(start))))
    start = int(m_Editor->wordStartPosition(start, true));
  if(end < m_Editor->length() && word(int(m_Editor->charAt(end - 1))) && word(int(m_Editor->charAt(end))))
    end = int(m_Editor->wordEndPosition(end, true));
  result.push_back(uint32_t(start));
  result.push_back(uint32_t(end));
  return result;
}

void ShaderCodeEditor::RemoveRangeAnchor(int32_t anchor)
{
  m_RangeAnchors.remove(anchor);
  m_ReplacementRanges.remove(anchor);
  m_CutRanges.remove(anchor);
}

void ShaderCodeEditor::TrackRangeEdit(int type, int position, int length, const QByteArray &text)
{
  const bool insert = (type & SC_MOD_INSERTTEXT) != 0;
  const bool history = (type & (SC_PERFORMED_UNDO | SC_PERFORMED_REDO)) != 0;
  const bool newAction = (type & SC_STARTACTION) != 0;
  if(history)
  {
    // An undone cut is no longer a move. Pasting its clipboard text later is
    // an ordinary copy and must not steal the restored original anchor.
    m_CutRanges.clear();
    m_CutText.clear();
  }
  if(!history && (newAction || m_RangeHistoryCursor <= 0 ||
                  m_RangeHistoryCursor > m_RangeHistory.size()))
  {
    if(m_RangeHistoryCursor < 0 || m_RangeHistoryCursor > m_RangeHistory.size())
    {
      m_RangeHistory.clear();
      m_RangeHistoryCursor = 0;
    }
    m_RangeHistory.resize(m_RangeHistoryCursor);
    RangeHistory entry;
    entry.before = m_RangeAnchors;
    m_RangeHistory.push_back(entry);
    m_RangeHistoryCursor++;
    m_ReplacementRanges.clear();
  }
  if(!insert && m_ClipboardAction == 1 && !history)
  {
    m_CutText = text;
    m_CutRanges.clear();
    for(auto it = m_RangeAnchors.constBegin(); it != m_RangeAnchors.constEnd(); ++it)
      if(it->valid && position <= it->start && it->end <= position + length)
      {
        RangeAnchor moved = it.value();
        moved.start -= position;
        moved.end -= position;
        m_CutRanges[it.key()] = moved;
      }
  }
  if(!insert && !history)
  {
    m_ReplacementRanges.clear();
    m_ReplacementPosition = position;
  }
  for(auto it = m_RangeAnchors.begin(); it != m_RangeAnchors.end(); ++it)
  {
    RangeAnchor &anchor = it.value();
    if(!anchor.valid)
      continue;
    if(insert)
    {
      auto word = [](int c) { return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                                    (c >= '0' && c <= '9') || c == '_'; };
      bool identifier = !text.isEmpty();
      for(char c : text)
        identifier = identifier && word(c);
      const bool prefix = identifier && position == anchor.start &&
                          word(int(m_Editor->charAt(position + length)));
      const bool suffix = identifier && position == anchor.end && position > 0 &&
                          word(int(m_Editor->charAt(position - 1)));
      if(position <= anchor.start && !prefix)
        anchor.start += length;
      if(position < anchor.end || suffix)
        anchor.end += length;
    }
    else
    {
      const bool replacement = !history && position >= anchor.start &&
                               position + length <= anchor.end;
      auto mapped = [position, length](int offset) {
        return offset <= position ? offset : offset >= position + length ? offset - length : position;
      };
      anchor.start = mapped(anchor.start);
      anchor.end = mapped(anchor.end);
      anchor.valid = anchor.start < anchor.end;
      if(replacement)
        m_ReplacementRanges[it.key()] = anchor;
    }
  }
  if(insert && !history && !newAction && position == m_ReplacementPosition)
  {
    for(auto it = m_ReplacementRanges.constBegin(); it != m_ReplacementRanges.constEnd(); ++it)
      if(m_RangeAnchors.contains(it.key()))
      {
        RangeAnchor replacement = it.value();
        replacement.end += length;
        replacement.valid = replacement.start < replacement.end;
        m_RangeAnchors[it.key()] = replacement;
      }
    m_ReplacementRanges.clear();
  }
  if(insert && !history && m_ClipboardAction == 3 && !m_CutText.isEmpty() && text == m_CutText)
  {
    for(auto it = m_CutRanges.constBegin(); it != m_CutRanges.constEnd(); ++it)
      if(m_RangeAnchors.contains(it.key()))
      {
        RangeAnchor moved = it.value();
        moved.start += position;
        moved.end += position;
        m_RangeAnchors[it.key()] = moved;
      }
    m_CutRanges.clear();
    m_CutText.clear();
  }
  if(history && (type & SC_LASTSTEPINUNDOREDO))
  {
    const bool undo = (type & SC_PERFORMED_UNDO) != 0;
    const int index = undo ? m_RangeHistoryCursor - 1 : m_RangeHistoryCursor;
    if(index >= 0 && index < m_RangeHistory.size())
    {
      const QMap<int, RangeAnchor> &saved = undo ? m_RangeHistory[index].before : m_RangeHistory[index].after;
      for(auto it = saved.constBegin(); it != saved.constEnd(); ++it)
        if(m_RangeAnchors.contains(it.key()))
          m_RangeAnchors[it.key()] = it.value();
    }
    // Keep the position even outside the retained journal. Otherwise redo of
    // an older evicted action could wrongly restore a newer retained range.
    m_RangeHistoryCursor += undo ? -1 : 1;
    m_ReplacementRanges.clear();
  }
  else if(!history && m_RangeHistoryCursor > 0)
  {
    m_RangeHistory[m_RangeHistoryCursor - 1].after = m_RangeAnchors;
    // Bound bookkeeping independently of Scintilla's text undo buffer. Older
    // edits still map positions; only restoration of a deleted range needs a
    // retained history entry. It never falls back to a same-named value.
    int entries = 0;
    for(const RangeHistory &entry : m_RangeHistory)
      entries += entry.before.size() + entry.after.size();
    while(m_RangeHistory.size() > 1 && (entries > 262144 || m_RangeHistory.size() > 4096))
    {
      entries -= m_RangeHistory.front().before.size() + m_RangeHistory.front().after.size();
      m_RangeHistory.removeFirst();
      m_RangeHistoryCursor--;
    }
  }
}

void ShaderCodeEditor::ClipboardAction(int action)
{
  if(action == 3)
  {
    const QClipboard *clipboard = QApplication::clipboard();
    const QMimeData *clipboardData = clipboard ? clipboard->mimeData() : NULL;
    if(!clipboardData || !clipboardData->hasText())
      return;
  }
  if(action == 1 || action == 2)
  {
    m_CutRanges.clear();
    m_CutText.clear();
  }
  m_ClipboardAction = action;
  if(action == 1)
    m_Editor->cut();
  else if(action == 2)
    m_Editor->copy();
  else if(action == 3)
    m_Editor->paste();
  m_ClipboardAction = 0;
}

uint32_t ShaderCodeEditor::GetCursorLine()
{
  return uint32_t(qMax(0, int(m_Editor->lineFromPosition(m_Editor->currentPos()))) + 1);
}

uint32_t ShaderCodeEditor::GetCursorColumn()
{
  return uint32_t(qMax(0, int(m_Editor->column(m_Editor->currentPos()))) + 1);
}

void ShaderCodeEditor::SetDiagnostics(const rdcarray<ShaderEditorDiagnostic> &diagnostics)
{
  if(!m_Services)
    return;

  QList<CodeEditorService::Diagnostic> converted;
  converted.reserve(int(diagnostics.size()));
  for(const ShaderEditorDiagnostic &value : diagnostics)
  {
    CodeEditorService::Diagnostic diagnostic;
    const int line = qBound(0, int(value.line) - 1, qMax(0, int(m_Editor->lineCount()) - 1));
    const int lineStart = int(m_Editor->positionFromLine(line));
    const int lineEnd = int(m_Editor->lineEndPosition(line));
    diagnostic.start = qBound(
        lineStart, int(m_Editor->findColumn(line, qMax(0, int(value.column) - 1))), lineEnd);
    // Compilers place "expected ';'"-style errors just past the last token, i.e.
    // on the line break, where an indicator has no glyph to underline and is
    // invisible. Like other IDEs, pull such a range back onto the last visible
    // character of the line.
    if(diagnostic.start >= lineEnd && lineEnd > lineStart)
    {
      // Cover the whole last token (identifier / closing brackets), not one
      // character, so the squiggle is actually noticeable.
      int from = lineEnd;
      const QByteArray text = m_Editor->textRange(lineStart, lineEnd);
      int index = text.size();
      while(index > 0 && QChar::fromLatin1(text[index - 1]).isSpace())
        index--;
      const int tokenEnd = index;
      while(index > 0 && tokenEnd - index < 40)
      {
        const char c = text[index - 1];
        const bool word = QChar::fromLatin1(c).isLetterOrNumber() || c == '_' || c == ')' ||
                          c == ']' || c == '(' || c == '.';
        if(!word)
          break;
        index--;
      }
      from = lineStart + (index < tokenEnd ? index : qMax(0, tokenEnd - 1));
      diagnostic.start = qBound(lineStart, from, lineEnd - 1);
      diagnostic.length = qMax(1, (lineStart + tokenEnd) - diagnostic.start);
    }
    else
    {
      diagnostic.length = qMax(1, qMin(int(value.length), qMax(1, lineEnd - diagnostic.start)));
    }
    diagnostic.line = line;
    diagnostic.column = diagnostic.start - lineStart;
    diagnostic.severity =
        value.severity == ShaderEditorDiagnosticSeverity::Warning
            ? CodeEditorService::DiagnosticSeverity::Warning
        : value.severity == ShaderEditorDiagnosticSeverity::Information
            ? CodeEditorService::DiagnosticSeverity::Information
        : value.severity == ShaderEditorDiagnosticSeverity::Hint
            ? CodeEditorService::DiagnosticSeverity::Hint
            : CodeEditorService::DiagnosticSeverity::Error;
    diagnostic.message = QString(value.message);
    diagnostic.source = QString(value.source);
    converted.push_back(diagnostic);
  }
  m_Services->setExternalDiagnostics(m_Editor, converted);
}

void ShaderCodeEditor::SetTextChangedCallback(EventCallback callback)
{
  m_TextChangedCallback = callback;
}

void ShaderCodeEditor::SetSaveCallback(EventCallback callback)
{
  m_SaveCallback = callback;
}

void ShaderCodeEditor::SetAddToChatCallback(SelectionCallback callback)
{
  m_AddToChatCallback = callback;
  UpdateAddToChatButton();
}

void ShaderCodeEditor::SetDefinitionCallback(DefinitionCallback callback)
{
  m_DefinitionCallback = callback;
}

void ShaderCodeEditor::SetVisualDebugCallback(VisualDebugCallback callback)
{
  m_VisualDebugCallback = callback;
  UpdateVisualDebugButton();
}

void ShaderCodeEditor::SetStopVisualDebugCallback(EventCallback callback)
{
  m_StopVisualDebugCallback = callback;
}

void ShaderCodeEditor::SetVisualDebugRange(uint32_t byteStart, uint32_t byteEnd, bool active)
{
  const sptr_t previous = m_Editor->indicatorCurrent();
  m_Editor->setIndicatorCurrent(VisualDebugExpressionIndicator);
  m_Editor->indicatorClearRange(0, m_Editor->length());
  m_Editor->markerDeleteAll(VisualDebugLineMarker);
  m_VisualDebugActive = active && byteStart < byteEnd && byteEnd <= uint32_t(m_Editor->length());
  if(m_VisualDebugActive)
  {
    m_Editor->indicatorFillRange(byteStart, byteEnd - byteStart);
    m_Editor->markerAdd(m_Editor->lineFromPosition(byteStart), VisualDebugLineMarker);
  }
  m_Editor->setIndicatorCurrent(previous);
}

void ShaderCodeEditor::RequestVisualDebug()
{
  RequestVisualDebugAt(int(m_Editor->currentPos()));
}

void ShaderCodeEditor::RequestVisualDebugAt(int position)
{
  if(!m_VisualDebugCallback || !m_NavigationEnabled || position < 0)
    return;
  int start = int(m_Editor->selectionStart());
  int end = int(m_Editor->selectionEnd());
  if(start == end || position < start || position > end)
  {
    start = int(m_Editor->wordStartPosition(position, true));
    end = int(m_Editor->wordEndPosition(position, true));
  }
  if(end > start)
    m_VisualDebugCallback(uint32_t(start), uint32_t(end));
  else
    SetStatusMessage(tr("Place the caret on a value, function name or return, or select an expression."));
}

void ShaderCodeEditor::UpdateVisualDebugButton()
{
  if(!m_VisualDebugButton) return;
  const int position = int(m_Editor->currentPos());
  if(!m_VisualDebugCallback || !m_NavigationEnabled || m_SelectingWithMouse ||
     !m_Editor->isVisible() ||
     (m_Editor->selectionStart() == m_Editor->selectionEnd() &&
      m_Editor->wordStartPosition(position, true) == m_Editor->wordEndPosition(position, true)))
  {
    m_VisualDebugButton->hide();
    return;
  }
  QWidget *viewport = m_Editor->viewport();
  const int yPosition = int(m_Editor->pointYFromPosition(position));
  if(!viewport || yPosition < 0 || yPosition >= viewport->height())
  {
    m_VisualDebugButton->hide();
    return;
  }
  m_VisualDebugButton->adjustSize();
  const QRect bounds(viewport->mapTo(this, QPoint(0, 0)), viewport->size());
  int x = bounds.left() + int(m_Editor->pointXFromPosition(position)) + 6;
  int y = bounds.top() + yPosition + int(m_Editor->textHeight(m_Editor->lineFromPosition(position))) + 4;
  if(m_AddToChatButton && m_AddToChatButton->isVisible())
  {
    x = m_AddToChatButton->x();
    y = m_AddToChatButton->y() + m_AddToChatButton->height() + 3;
  }
  if(y + m_VisualDebugButton->height() > bounds.bottom() - 4)
    y = bounds.top() + yPosition - m_VisualDebugButton->height() - 4;
  m_VisualDebugButton->move(qBound(bounds.left() + 4, x, qMax(bounds.left() + 4, bounds.right() - m_VisualDebugButton->width() - 4)),
                            qBound(bounds.top() + 4, y, qMax(bounds.top() + 4, bounds.bottom() - m_VisualDebugButton->height() - 4)));
  m_VisualDebugButton->show();
  m_VisualDebugButton->raise();
}

void ShaderCodeEditor::ClearCallbacks()
{
  m_TextChangedCallback = EventCallback();
  m_SaveCallback = EventCallback();
  m_AddToChatCallback = SelectionCallback();
  m_DefinitionCallback = DefinitionCallback();
  m_VisualDebugCallback = VisualDebugCallback();
  m_StopVisualDebugCallback = EventCallback();
  SetVisualDebugRange(0, 0, false);
  UpdateVisualDebugButton();
}

void ShaderCodeEditor::GoToPosition(int position)
{
  const int bounded = qBound(0, position, int(m_Editor->length()));
  m_Editor->gotoPos(bounded);
  m_Editor->scrollCaret();
  m_Editor->QWidget::setFocus(Qt::OtherFocusReason);
}

void ShaderCodeEditor::OpenDefinition(const QString &fileName, int line, int column)
{
  if(m_DefinitionCallback)
    m_DefinitionCallback(fileName, uint32_t(qMax(0, line) + 1), uint32_t(qMax(0, column) + 1));
}

void ShaderCodeEditor::SetStatusMessage(const QString &message)
{
  m_Editor->setToolTip(message);
  if(!message.isEmpty())
    QToolTip::showText(QCursor::pos(), message, m_Editor);
}

void ShaderCodeEditor::SetDiagnosticSummary(int errors, int warnings, const QString &details,
                                           int firstLine)
{
  m_FirstDiagnosticLine = qMax(1, firstLine);
  m_DiagnosticSummary->setText(tr("Errors: %1 · Warnings: %2").arg(errors).arg(warnings));
  m_DiagnosticSummary->setToolTip(details);
  m_DiagnosticSummary->setVisible(errors + warnings > 0);
}

void ShaderCodeEditor::RegisterDocument()
{
  if(m_Services && m_NavigationEnabled)
    m_Services->registerEditor(m_Editor, m_FileName, m_Language, m_IncludeBaseDirectory);
}

void ShaderCodeEditor::UpdateLineNumberMargin()
{
  const int digits = qMax(MinimumLineNumberDigits, QString::number(m_Editor->lineCount()).size());
  const QString sample(digits + 1, QLatin1Char('9'));
  m_Editor->setMarginWidthN(0, m_Editor->textWidth(STYLE_LINENUMBER, sample.toUtf8().constData()));
}

bool ShaderCodeEditor::eventFilter(QObject *watched, QEvent *event)
{
  if(watched == m_Editor && event &&
     (event->type() == QEvent::ShortcutOverride || event->type() == QEvent::KeyPress))
  {
    QKeyEvent *keyEvent = static_cast<QKeyEvent *>(event);
    const Qt::KeyboardModifiers modifiers = keyEvent->modifiers();
    const int clipboardAction = keyEvent->matches(QKeySequence::Cut) ? 1 :
                                keyEvent->matches(QKeySequence::Copy) ? 2 :
                                keyEvent->matches(QKeySequence::Paste) ? 3 : 0;
    if(clipboardAction)
    {
      if(event->type() == QEvent::KeyPress)
        ClipboardAction(clipboardAction);
      event->accept();
      return true;
    }
    if(keyEvent->key() == Qt::Key_E &&
       (modifiers == Qt::ControlModifier ||
        modifiers == (Qt::ControlModifier | Qt::ShiftModifier)))
    {
      // Handle editor commands before Scintilla and prevent a parent action from
      // taking them. Ctrl+D keeps its normal duplicate-selection/line behavior.
      if(event->type() == QEvent::KeyPress)
      {
        if(modifiers & Qt::ShiftModifier)
        {
          if(m_StopVisualDebugCallback)
            m_StopVisualDebugCallback();
        }
        else
        {
          RequestVisualDebug();
        }
      }
      event->accept();
      return true;
    }
  }
  return QWidget::eventFilter(watched, event);
}

void ShaderCodeEditor::HandleKeyPress(QKeyEvent *event)
{
  if(!event)
    return;

  if(event->matches(QKeySequence::Save))
  {
    if(m_SaveCallback)
      m_SaveCallback();
    event->accept();
  }
  else if(event->matches(QKeySequence::Find))
  {
    ShowFind(false);
    event->accept();
  }
  else if(event->matches(QKeySequence::Replace))
  {
    ShowFind(true);
    event->accept();
  }
  else if(event->key() == Qt::Key_F3)
  {
    FindNext(!(event->modifiers() & Qt::ShiftModifier));
    event->accept();
  }
  else if(event->key() == Qt::Key_G && event->modifiers() == Qt::ControlModifier)
  {
    bool accepted = false;
    const int line = QInputDialog::getInt(this, tr("Go to Line"), tr("Line:"),
                                          int(GetCursorLine()), 1,
                                          qMax(1, int(m_Editor->lineCount())), 1, &accepted);
    if(accepted)
      GoToLine(uint32_t(line));
    event->accept();
  }
  else if(event->key() == Qt::Key_Escape && m_FindReplace->isVisible())
  {
    CloseFind();
    event->accept();
  }
}

void ShaderCodeEditor::ShowFind(bool replace)
{
  const QByteArray selected = m_Editor->getSelText();
  if(!selected.isEmpty() && !selected.contains('\n'))
    m_FindReplace->setFindText(QString::fromUtf8(selected));
  m_FindReplace->setReplaceMode(replace);
  m_FindReplace->show();
  m_FindReplace->takeFocus();
  UpdateFindCount();
}

void ShaderCodeEditor::CloseFind()
{
  m_FindReplace->hide();
  m_Editor->QWidget::setFocus(Qt::OtherFocusReason);
}

int ShaderCodeEditor::SearchFlags() const
{
  int flags = 0;
  if(m_FindReplace->matchCase())
    flags |= SCFIND_MATCHCASE;
  if(m_FindReplace->matchWord())
    flags |= SCFIND_WHOLEWORD;
  if(m_FindReplace->regexp())
    flags |= SCFIND_REGEXP | SCFIND_POSIX;
  return flags;
}

void ShaderCodeEditor::FindNext(bool down)
{
  const QByteArray needle = m_FindReplace->findText().toUtf8();
  if(needle.isEmpty())
  {
    m_FindReplace->setResultCount(0, 0);
    return;
  }

  const int start = down ? int(m_Editor->selectionEnd()) : int(m_Editor->selectionStart());
  m_Editor->setStatus(SC_STATUS_OK);
  QPair<int, int> result =
      m_Editor->findText(SearchFlags(), needle.constData(), start, down ? int(m_Editor->length()) : 0);
  if(m_Editor->status() == SC_STATUS_WARN_REGEX)
  {
    m_FindReplace->setSearchError(tr("Invalid expression"));
    m_FindReplace->setResultCount(0, 0);
    return;
  }
  if(result.first < 0)
    result = m_Editor->findText(SearchFlags(), needle.constData(),
                                down ? 0 : int(m_Editor->length()), down ? start : start);
  if(result.first >= 0)
  {
    m_Editor->setSelection(result.second, result.first);
    m_Editor->scrollCaret();
  }
  UpdateFindCount();
}

void ShaderCodeEditor::FindIncremental()
{
  const QByteArray needle = m_FindReplace->findText().toUtf8();
  if(needle.isEmpty())
  {
    m_FindReplace->setSearchError(QString());
    m_FindReplace->setResultCount(0, 0);
    return;
  }

  const int start = int(m_Editor->selectionStart());
  m_Editor->setStatus(SC_STATUS_OK);
  QPair<int, int> result =
      m_Editor->findText(SearchFlags(), needle.constData(), start, int(m_Editor->length()));
  if(m_Editor->status() == SC_STATUS_WARN_REGEX)
  {
    m_FindReplace->setSearchError(tr("Invalid expression"));
    m_FindReplace->setResultCount(0, 0);
    return;
  }
  if(result.first < 0 && start > 0)
    result = m_Editor->findText(SearchFlags(), needle.constData(), 0, start);
  if(result.first >= 0)
  {
    m_Editor->setSelection(result.second, result.first);
    m_Editor->scrollCaret();
  }
  UpdateFindCount();
}

void ShaderCodeEditor::UpdateFindCount()
{
  const QByteArray needle = m_FindReplace->findText().toUtf8();
  if(needle.isEmpty())
  {
    m_FindReplace->setResultCount(0, 0);
    m_FindReplace->setSearchError(QString());
    return;
  }

  int total = 0;
  int current = 0;
  int position = 0;
  const int selectionStart = int(m_Editor->selectionStart());
  m_Editor->setStatus(SC_STATUS_OK);
  while(position <= m_Editor->length() && total < 100000)
  {
    const QPair<int, int> result = m_Editor->findText(
        SearchFlags(), needle.constData(), position, int(m_Editor->length()));
    if(m_Editor->status() == SC_STATUS_WARN_REGEX)
    {
      m_FindReplace->setSearchError(tr("Invalid expression"));
      m_FindReplace->setResultCount(0, 0);
      return;
    }
    if(result.first < 0)
      break;
    total++;
    if(result.first <= selectionStart)
      current = total;
    if(result.second > result.first)
      position = result.second;
    else
    {
      const int advanced = int(m_Editor->positionAfter(result.first));
      if(advanced <= result.first)
        break;
      position = advanced;
    }
  }
  m_FindReplace->setResultCount(current, total);
  m_FindReplace->setSearchError(QString());
}

void ShaderCodeEditor::ReplaceCurrent()
{
  if(IsReadOnly())
    return;

  const QByteArray needle = m_FindReplace->findText().toUtf8();
  if(needle.isEmpty())
    return;
  const int selectionStart = int(m_Editor->selectionStart());
  const int selectionEnd = int(m_Editor->selectionEnd());
  m_Editor->setStatus(SC_STATUS_OK);
  m_Editor->setSearchFlags(SearchFlags());
  m_Editor->setTargetRange(selectionStart, selectionEnd);
  const int found = int(m_Editor->searchInTarget(needle.size(), needle.constData()));
  if(m_Editor->status() == SC_STATUS_WARN_REGEX)
  {
    m_FindReplace->setSearchError(tr("Invalid expression"));
    return;
  }
  if(found != selectionStart || m_Editor->targetEnd() != selectionEnd)
  {
    FindNext(true);
    return;
  }

  const QByteArray replacement = m_FindReplace->replaceText().toUtf8();
  if(m_FindReplace->regexp())
    m_Editor->replaceTargetRE(replacement.size(), replacement.constData());
  else
    m_Editor->replaceTarget(replacement.size(), replacement.constData());
  m_Editor->setEmptySelection(m_Editor->targetEnd());
  FindNext(true);
}

void ShaderCodeEditor::ReplaceAll()
{
  if(IsReadOnly())
    return;
  const QByteArray needle = m_FindReplace->findText().toUtf8();
  const QByteArray replacement = m_FindReplace->replaceText().toUtf8();
  if(needle.isEmpty())
    return;

  m_Editor->beginUndoAction();
  m_Editor->setStatus(SC_STATUS_OK);
  m_Editor->setSearchFlags(SearchFlags());
  int position = 0;
  int replacements = 0;
  while(position <= m_Editor->length() && replacements < 100000)
  {
    m_Editor->setTargetStart(position);
    m_Editor->setTargetEnd(m_Editor->length());
    const int found = int(m_Editor->searchInTarget(needle.size(), needle.constData()));
    if(m_Editor->status() == SC_STATUS_WARN_REGEX)
    {
      m_Editor->endUndoAction();
      m_FindReplace->setSearchError(tr("Invalid expression"));
      return;
    }
    if(found < 0)
      break;
    const int oldEnd = int(m_Editor->targetEnd());
    if(m_FindReplace->regexp())
      m_Editor->replaceTargetRE(replacement.size(), replacement.constData());
    else
      m_Editor->replaceTarget(replacement.size(), replacement.constData());
    position = int(m_Editor->targetEnd());
    if(oldEnd == found && position <= found)
    {
      const int advanced = int(m_Editor->positionAfter(found));
      if(advanced <= found)
        break;
      position = advanced;
    }
    replacements++;
  }
  m_Editor->endUndoAction();
  UpdateFindCount();
}

void ShaderCodeEditor::ShowContextMenu(const QPoint &position)
{
  QMenu *menu = new QMenu(this);
  menu->setAttribute(Qt::WA_DeleteOnClose);
  QAction *undo = menu->addAction(tr("Undo"), m_Editor, &ScintillaEdit::undo);
  QAction *redo = menu->addAction(tr("Redo"), m_Editor, &ScintillaEdit::redo);
  undo->setEnabled(!IsReadOnly() && m_Editor->canUndo());
  redo->setEnabled(!IsReadOnly() && m_Editor->canRedo());
  menu->addSeparator();
  QAction *cut = menu->addAction(tr("Cut"), this, [this]() { ClipboardAction(1); });
  QAction *copy = menu->addAction(tr("Copy"), this, [this]() { ClipboardAction(2); });
  QAction *paste = menu->addAction(tr("Paste"), this, [this]() { ClipboardAction(3); });
  cut->setEnabled(!IsReadOnly() && m_Editor->selectionEnd() > m_Editor->selectionStart());
  copy->setEnabled(m_Editor->selectionEnd() > m_Editor->selectionStart());
  const QClipboard *clipboard = QApplication::clipboard();
  const QMimeData *clipboardData = clipboard ? clipboard->mimeData() : NULL;
  paste->setEnabled(!IsReadOnly() && clipboardData && clipboardData->hasText());
  menu->addAction(tr("Select All"), m_Editor, &ScintillaEdit::selectAll);
  menu->addSeparator();
  QAction *definition = menu->addAction(tr("Go to Definition"));
  definition->setShortcut(QKeySequence(Qt::Key_F12));
  definition->setEnabled(m_Services != NULL);
  QObject::connect(definition, &QAction::triggered, this, [this]() {
    if(m_Services)
      m_Services->requestDefinition(m_Editor);
  });
  QAction *inspect = menu->addAction(tr("Inspect Symbol"));
  const int symbolPosition = int(m_Editor->positionFromPointClose(position.x(), position.y()));
  inspect->setEnabled(m_Services != NULL && symbolPosition >= 0);
  QObject::connect(inspect, &QAction::triggered, this, [this, symbolPosition]() {
    if(!m_Services) return;
    const CodeEditorService::SymbolInspection inspection =
        m_Services->inspectSymbol(m_Editor, symbolPosition);
    if(!inspection.isValid())
    {
      SetStatusMessage(tr("No symbol was found at this position."));
      return;
    }
    const QString sourceSha256 = inspection.symbol.sourceSha256;
    QMenu *details = new QMenu(this);
    details->setAttribute(Qt::WA_DeleteOnClose);
    details->addSection(tr("Symbol: %1").arg(inspection.symbol.name));
    details->addAction(tr("Declaration: line %1").arg(inspection.symbol.declaration.line + 1))
        ->setEnabled(false);
    if(inspection.symbol.global)
      details->addAction(tr("Scope: global"))->setEnabled(false);
    else
      details->addAction(tr("Lexical scope: lines %1-%2")
                             .arg(m_Editor->lineFromPosition(inspection.symbol.scopeStart) + 1)
                             .arg(m_Editor->lineFromPosition(inspection.symbol.scopeEnd) + 1))
          ->setEnabled(false);
    details->addAction(tr("Source SHA-256: %1").arg(sourceSha256.left(12)))
        ->setEnabled(false);
    details->addSection(tr("References in this document"));
    for(const CodeEditorService::Location &reference : inspection.references)
    {
      const int start = int(m_Editor->positionFromLine(reference.line));
      const int end = int(m_Editor->lineEndPosition(reference.line));
      const QString snippet = QString::fromUtf8(m_Editor->textRange(start, end)).simplified().left(80);
      QAction *action = details->addAction(tr("Line %1: %2").arg(reference.line + 1).arg(snippet));
      QObject::connect(action, &QAction::triggered, this, [this, sourceSha256, reference]() {
        if(m_Services && m_Services->documentSourceSha256(m_Editor) == sourceSha256)
          GoToPosition(reference.position);
        else
          SetStatusMessage(tr("Symbol index changed; inspect it again."));
      });
    }
    if(inspection.references.isEmpty())
      details->addAction(tr("No references found"))->setEnabled(false);
    else if(inspection.referencesTruncated)
      details->addAction(tr("Showing the first 64 references"))->setEnabled(false);
    details->popup(QCursor::pos());
  });
  QAction *complete = menu->addAction(tr("Trigger Completion"));
  complete->setShortcut(QKeySequence(Qt::CTRL | Qt::Key_Space));
  complete->setEnabled(m_Services != NULL);
  QObject::connect(complete, &QAction::triggered, this, [this]() {
    if(m_Services)
      m_Services->requestCompletion(m_Editor, true);
  });
  QAction *addToChat = menu->addAction(tr("Add to chat"));
  addToChat->setEnabled(m_AddToChatCallback &&
                        m_Editor->selectionEnd() > m_Editor->selectionStart());
  QObject::connect(addToChat, &QAction::triggered, this, &ShaderCodeEditor::AddSelectionToChat);
  QAction *visualDebug = menu->addAction(tr("Output to RT"));
  visualDebug->setShortcut(QKeySequence(Qt::CTRL | Qt::Key_E));
  visualDebug->setEnabled(bool(m_VisualDebugCallback) && m_NavigationEnabled && symbolPosition >= 0);
  QObject::connect(visualDebug, &QAction::triggered, this,
                   [this, symbolPosition]() { RequestVisualDebugAt(symbolPosition); });
  QAction *stopDebug = menu->addAction(tr("Stop visual debug"));
  stopDebug->setShortcut(QKeySequence(Qt::CTRL | Qt::SHIFT | Qt::Key_E));
  stopDebug->setEnabled(bool(m_StopVisualDebugCallback));
  QObject::connect(stopDebug, &QAction::triggered, this, [this]() {
    if(m_StopVisualDebugCallback) m_StopVisualDebugCallback();
  });
  menu->popup(m_Editor->viewport()->mapToGlobal(position));
}

void ShaderCodeEditor::UpdateAddToChatButton()
{
  if(!m_AddToChatButton)
    return;
  if(m_SelectingWithMouse || !m_AddToChatCallback || !m_Editor->isVisible() ||
     m_Editor->selectionEnd() <= m_Editor->selectionStart())
  {
    m_AddToChatButton->hide();
    return;
  }
  PositionAddToChatButton();
}

void ShaderCodeEditor::PositionAddToChatButton()
{
  if(!m_AddToChatButton || m_SelectingWithMouse || !m_AddToChatCallback ||
     m_Editor->selectionEnd() <= m_Editor->selectionStart())
    return;

  QWidget *viewport = m_Editor->viewport();
  if(!viewport)
    return;

  // Anchor to the caret: it sits at the end of a drag selection and at the moving
  // end of a keyboard selection, which is where the user is looking.
  const sptr_t caret = m_Editor->currentPos();
  const int line = int(m_Editor->lineFromPosition(caret));
  const QPoint caretPoint(int(m_Editor->pointXFromPosition(caret)),
                          int(m_Editor->pointYFromPosition(caret)));
  if(caretPoint.y() < 0 || caretPoint.y() >= viewport->height())
  {
    m_AddToChatButton->hide();
    return;
  }

  m_AddToChatButton->adjustSize();
  const QRect viewportRect(viewport->mapTo(this, QPoint(0, 0)), viewport->size());
  int x = viewportRect.left() + caretPoint.x() + 6;
  int y = viewportRect.top() + caretPoint.y() + int(m_Editor->textHeight(line)) + 4;
  if(y + m_AddToChatButton->height() > viewportRect.bottom() - 4)
    y = viewportRect.top() + caretPoint.y() - m_AddToChatButton->height() - 4;
  const int maxX = qMax(viewportRect.left() + 4,
                        viewportRect.right() - m_AddToChatButton->width() - 4);
  const int maxY = qMax(viewportRect.top() + 4,
                        viewportRect.bottom() - m_AddToChatButton->height() - 4);
  m_AddToChatButton->move(qBound(viewportRect.left() + 4, x, maxX),
                          qBound(viewportRect.top() + 4, y, maxY));
  m_AddToChatButton->show();
  m_AddToChatButton->raise();
}

void ShaderCodeEditor::AddSelectionToChat()
{
  if(!m_AddToChatCallback)
    return;
  const int start = int(m_Editor->selectionStart());
  const int end = int(m_Editor->selectionEnd());
  if(end <= start)
    return;
  const QString text = QString::fromUtf8(m_Editor->textRange(start, end));
  const uint32_t startLine = uint32_t(m_Editor->lineFromPosition(start) + 1);
  const sptr_t finalCharacter = m_Editor->positionBefore(end);
  const uint32_t endLine = uint32_t(m_Editor->lineFromPosition(finalCharacter) + 1);
  QVariantMap range;
  range[QLatin1String("columnStart")] = int(m_Editor->column(start)) + 1;
  range[QLatin1String("columnEnd")] = int(m_Editor->column(finalCharacter)) + 1;
  setProperty("breadSelectionRange", range);
  m_AddToChatCallback(text, m_FileName, startLine, endLine);
}
