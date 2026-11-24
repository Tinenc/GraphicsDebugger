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

#include "CodeEditorService.h"
#include "BackgroundProcess.h"

#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDir>
#include <QEvent>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QKeyEvent>
#include <QMenu>
#include <QAction>
#include <QPointer>
#include <QPalette>
#include <QProcess>
#include <QRegularExpression>
#include <QSet>
#include <QStandardPaths>
#include <QTimer>
#include <QUrl>
#include <algorithm>
#include <cctype>
#include <limits>
#include <memory>

#include "scintilla/include/Scintilla.h"
#include "scintilla/include/qt/ScintillaEdit.h"

namespace
{
const int ErrorIndicator = 8;
const int WarningIndicator = 9;
const int SymbolOccurrenceIndicator = 10;
const int FunctionNameIndicator = 11;
const int VariableNameIndicator = 12;
const int SemanticNameIndicator = 13;
const int DiagnosticsDebounceMS = 160;
const int SemanticTokensDebounceMS = 350;
const int SemanticPaintBatchSize = 512;
const int MaxCompletionItems = 256;
const int MaxDiagnostics = 2000;
const int MaxMessageBytes = 4 * 1024 * 1024;
const int MaxHeaderBytes = 16 * 1024;
const int NavigationHistoryLimit = 128;

struct SmartNewlineEdit
{
  QByteArray replacement;
  int caretOffset = 0;
};

struct TextReplacement
{
  int start = 0;
  int removedLength = 0;
  QByteArray inserted;
};

bool isHorizontalSpace(char c)
{
  return c == ' ' || c == '\t';
}

QByteArray leadingWhitespace(const QByteArray &text)
{
  int length = 0;
  while(length < text.size() && isHorizontalSpace(text[length]))
    length++;
  return text.left(length);
}

char lastNonWhitespace(const QByteArray &text)
{
  for(int i = text.size() - 1; i >= 0; i--)
    if(!isHorizontalSpace(text[i]) && text[i] != '\r')
      return text[i];
  return '\0';
}

char firstNonWhitespace(const QByteArray &text)
{
  for(char c : text)
    if(!isHorizontalSpace(c) && c != '\r')
      return c;
  return '\0';
}

SmartNewlineEdit planSmartNewline(const QByteArray &text, int replaceStart, int replaceEnd,
                                  const QByteArray &eol, const QByteArray &indentUnit,
                                  bool indentAfterColon)
{
  SmartNewlineEdit ret;
  replaceStart = qBound(0, replaceStart, text.size());
  replaceEnd = qBound(replaceStart, replaceEnd, text.size());

  const int previousLF = replaceStart > 0 ? text.lastIndexOf('\n', replaceStart - 1) : -1;
  const int previousCR = replaceStart > 0 ? text.lastIndexOf('\r', replaceStart - 1) : -1;
  const int currentLineStart = qMax(previousLF, previousCR) + 1;
  const int nextLF = text.indexOf('\n', replaceEnd);
  const int nextCR = text.indexOf('\r', replaceEnd);
  const int currentLineEnd = nextLF < 0   ? (nextCR < 0 ? text.size() : nextCR)
                             : nextCR < 0 ? nextLF
                                          : qMin(nextLF, nextCR);
  const QByteArray before = text.mid(currentLineStart, replaceStart - currentLineStart);
  const QByteArray after = text.mid(replaceEnd, currentLineEnd - replaceEnd);
  const QByteArray indentation = leadingWhitespace(before);
  const char previous = lastNonWhitespace(before);
  const char next = firstNonWhitespace(after);

  if(previous == '{' && next == '}')
  {
    ret.replacement = eol + indentation + indentUnit + eol + indentation;
    ret.caretOffset = eol.size() + indentation.size() + indentUnit.size();
  }
  else
  {
    const bool increaseIndent = previous == '{' || previous == '[' || previous == '(' ||
                                (indentAfterColon && previous == ':');
    ret.replacement = eol + indentation + (increaseIndent ? indentUnit : QByteArray());
    ret.caretOffset = ret.replacement.size();
  }
  return ret;
}

QByteArray eolBytes(int mode)
{
  if(mode == SC_EOL_CRLF)
    return QByteArray("\r\n");
  if(mode == SC_EOL_CR)
    return QByteArray("\r");
  return QByteArray("\n");
}

int transformedPosition(int position, const QList<TextReplacement> &replacements)
{
  const int originalPosition = position;
  int cumulativeDelta = 0;
  for(const TextReplacement &replacement : replacements)
  {
    const int end = replacement.start + replacement.removedLength;
    if(originalPosition < replacement.start)
      continue;
    if(originalPosition <= end)
      return replacement.start + cumulativeDelta + replacement.inserted.size();
    else
      cumulativeDelta += replacement.inserted.size() - replacement.removedLength;
  }
  return originalPosition + cumulativeDelta;
}

bool hasPrimaryModifier(Qt::KeyboardModifiers modifiers, bool allowShift)
{
  modifiers &= ~Qt::KeypadModifier;
  if(allowShift)
    modifiers &= ~Qt::ShiftModifier;
  return modifiers == Qt::ControlModifier || modifiers == Qt::MetaModifier;
}

long scintillaColour(int r, int g, int b)
{
  return long(r) | (long(g) << 8) | (long(b) << 16);
}

bool isIdentifierByte(char c)
{
  return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_';
}

bool isIdentifierStartByte(char c)
{
  return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_';
}

QPair<int, int> identifierRange(const QByteArray &text, int position)
{
  position = qBound(0, position, text.size());

  int start = position;
  if(start == text.size() || (start > 0 && !isIdentifierByte(text[start])))
    start--;

  while(start >= 0 && isIdentifierByte(text[start]))
    start--;
  start++;

  int end = qMax(start, position);
  while(end < text.size() && isIdentifierByte(text[end]))
    end++;

  if(start >= end || start < 0 || !isIdentifierStartByte(text[start]))
    return qMakePair(position, position);

  return qMakePair(start, end);
}

QString identifierAt(const QByteArray &text, int position, QPair<int, int> *range = NULL)
{
  const QPair<int, int> r = identifierRange(text, position);
  if(range)
    *range = r;
  return QString::fromUtf8(text.mid(r.first, r.second - r.first));
}

int lineStart(const QByteArray &text, int line)
{
  if(line <= 0)
    return 0;

  int currentLine = 0;
  for(int i = 0; i < text.size(); i++)
  {
    if(text[i] == '\n' && ++currentLine == line)
      return i + 1;
  }

  return text.size();
}

int lineEnd(const QByteArray &text, int start)
{
  const int newline = text.indexOf('\n', qBound(0, start, text.size()));
  return newline < 0 ? text.size() : newline;
}

QJsonObject lspPosition(const QByteArray &text, int bytePosition)
{
  bytePosition = qBound(0, bytePosition, text.size());
  int line = 0;
  int start = 0;
  for(int i = 0; i < bytePosition; i++)
  {
    if(text[i] == '\n')
    {
      line++;
      start = i + 1;
    }
  }

  // LSP 3.x defaults to UTF-16 code units. QString::size() has exactly that unit on Qt 5.
  const int character = QString::fromUtf8(text.mid(start, bytePosition - start)).size();
  QJsonObject ret;
  ret[QStringLiteral("line")] = line;
  ret[QStringLiteral("character")] = character;
  return ret;
}

QJsonObject lspContentChange(int syncKind, const QByteArray &previousText,
                            const QByteArray &text)
{
  QJsonObject change;
  // Incremental providers require a range in the OLD synchronized document. In particular,
  // slangd treats a missing range as an insertion, so repeatedly sending the complete file
  // without a range accumulates duplicate types/functions and thousands of false diagnostics.
  // Replacing one whole range retains the existing debounce without deriving individual edits.
  if(syncKind == 2)
  {
    QJsonObject range;
    range[QStringLiteral("start")] = lspPosition(previousText, 0);
    range[QStringLiteral("end")] = lspPosition(previousText, previousText.size());
    change[QStringLiteral("range")] = range;
  }
  change[QStringLiteral("text")] = QString::fromUtf8(text);
  return change;
}

bool slangNeedsWholeDocumentReopen(const QByteArray &previousText)
{
  // Slang 2026.20's EOF boundary map stores a UTF-8 byte count where getOffset expects a
  // code-point index. A non-ASCII last line without a newline therefore leaves that line
  // behind during a full-range replacement. Reopening uses the provider's normal setText
  // path and preserves the user's exact buffer, including its final newline convention.
  const QByteArray lastLine = previousText.mid(previousText.lastIndexOf('\n') + 1);
  return QString::fromUtf8(lastLine).size() != lastLine.size();
}

int bytePosition(const QByteArray &text, const QJsonObject &position)
{
  const int line = qMax(0, position.value(QStringLiteral("line")).toInt());
  const int character = qMax(0, position.value(QStringLiteral("character")).toInt());
  const int start = lineStart(text, line);
  const int end = lineEnd(text, start);
  const QString lineText = QString::fromUtf8(text.mid(start, end - start));
  return start + lineText.left(qMin(character, lineText.size())).toUtf8().size();
}

int utf16IndexToByte(const QString &text, int utf16Index)
{
  return text.left(qBound(0, utf16Index, text.size())).toUtf8().size();
}

struct SemanticColorRange
{
  int start = 0;
  int length = 0;
  long color = 0;
};

long semanticTokenColor(const QString &type, bool dark)
{
  if(type == QStringLiteral("function") || type == QStringLiteral("method"))
    return dark ? scintillaColour(57, 204, 155) : scintillaColour(0, 112, 82);
  if(type == QStringLiteral("variable"))
    return dark ? scintillaColour(156, 205, 254) : scintillaColour(38, 78, 130);
  if(type == QStringLiteral("parameter"))
    return dark ? scintillaColour(230, 191, 139) : scintillaColour(130, 78, 24);
  if(type == QStringLiteral("property"))
    return dark ? scintillaColour(185, 174, 240) : scintillaColour(103, 62, 152);
  if(type == QStringLiteral("type") || type == QStringLiteral("class") ||
     type == QStringLiteral("struct") || type == QStringLiteral("interface") ||
     type == QStringLiteral("enum") || type == QStringLiteral("typeParameter") ||
     type == QStringLiteral("namespace"))
    return dark ? scintillaColour(78, 201, 176) : scintillaColour(0, 110, 105);
  if(type == QStringLiteral("enumMember") || type == QStringLiteral("macro"))
    return dark ? scintillaColour(212, 153, 220) : scintillaColour(135, 50, 130);
  // Keywords, comments, strings and numbers remain the lexer's responsibility.
  return -1;
}

bool decodeSemanticColorRanges(const QByteArray &text, const QJsonArray &data,
                               const QStringList &legend, bool dark,
                               QVector<SemanticColorRange> &ranges)
{
  ranges.clear();
  if(data.size() % 5 != 0 || data.size() > 1000000 || text.size() > MaxMessageBytes)
    return false;
  const QString source = QString::fromUtf8(text);
  QVector<int> lineStarts;
  lineStarts.push_back(0);
  for(int i = 0; i < source.size(); ++i)
    if(source[i] == QLatin1Char('\n')) lineStarts.push_back(i + 1);
  int line = 0, character = 0, previousEnd = 0;
  int mappedLine = -1, lineByteStart = 0, utf16Start = 0;
  ranges.reserve(data.size() / 5);
  for(int index = 0; index < data.size(); index += 5)
  {
    int values[5];
    for(int part = 0; part < 5; ++part)
    {
      const QJsonValue value = data[index + part];
      const double number = value.toDouble(-1);
      if(!value.isDouble() || number < 0 || number > std::numeric_limits<int>::max() ||
         number != double(int(number)))
        return false;
      values[part] = int(number);
    }
    if(values[0] > lineStarts.size() - 1 - line) return false;
    line += values[0];
    if(values[0] > 0) character = 0;
    if(values[1] > std::numeric_limits<int>::max() - character) return false;
    character += values[1];
    if(values[3] >= legend.size()) return false;
    const long color = semanticTokenColor(legend[values[3]], dark);
    // A relocated include projects its path only in the language-server text. Such
    // string/keyword tokens are intentionally left to the lexer in the real document.
    if(color < 0) continue;
    const int start = lineStarts[line];
    int end = line + 1 < lineStarts.size() ? lineStarts[line + 1] - 1 : source.size();
    if(end > start && source[end - 1] == QLatin1Char('\r')) --end;
    if(character > end - start || values[2] <= 0 || values[2] > end - start - character ||
       values[3] >= legend.size())
      return false;
    const int tokenStart = start + character, tokenEnd = tokenStart + values[2];
    if(tokenStart < previousEnd ||
       (tokenStart > 0 && source[tokenStart].isLowSurrogate()) ||
       (tokenEnd < source.size() && source[tokenEnd].isLowSurrogate()))
      return false;
    previousEnd = tokenEnd;
    // Advance once through UTF-16 instead of converting the whole prefix per token.
    // A large Shader has many identifiers: quadratic prefix conversion freezes its UI.
    if(mappedLine != line)
    {
      lineByteStart += source.mid(utf16Start, start - utf16Start).toUtf8().size();
      utf16Start = start;
      mappedLine = line;
    }
    const int byteStart = lineByteStart + source.mid(start, character).toUtf8().size();
    const int byteLength = source.mid(tokenStart, values[2]).toUtf8().size();
    ranges.push_back({byteStart, byteLength, color});
  }
  return true;
}

QString normaliseLanguage(const QString &language, const QString &fileName)
{
  QString ret = language.trimmed().toLower();
  if(ret == QStringLiteral("c++") || ret == QStringLiteral("cpp"))
    return QStringLiteral("cpp");
  if(ret == QStringLiteral("hlsl") || ret == QStringLiteral("glsl") ||
     ret == QStringLiteral("slang") || ret == QStringLiteral("python"))
    return ret;

  const QString suffix = QFileInfo(fileName).suffix().toLower();
  if(suffix == QStringLiteral("hlsl") || suffix == QStringLiteral("hlsli") ||
     suffix == QStringLiteral("fx") || suffix == QStringLiteral("fxh"))
    return QStringLiteral("hlsl");
  if(suffix == QStringLiteral("glsl") || suffix == QStringLiteral("vert") ||
     suffix == QStringLiteral("frag") || suffix == QStringLiteral("geom") ||
     suffix == QStringLiteral("comp"))
    return QStringLiteral("glsl");
  if(suffix == QStringLiteral("slang"))
    return QStringLiteral("slang");
  if(suffix == QStringLiteral("py"))
    return QStringLiteral("python");
  return ret.isEmpty() ? QStringLiteral("hlsl") : ret;
}

QStringList languageKeywords(const QString &language)
{
  const QString common = QStringLiteral(
      "break case const continue default discard do else false for if in inout out return static "
      "struct switch true while void bool int uint half float double matrix vector namespace "
      "typedef using class enum cbuffer register packoffset row_major column_major precise "
      "groupshared "
      "linear centroid nointerpolation noperspective sample globallycoherent uniform volatile");
  QStringList ret = common.split(QLatin1Char(' '), QString::SkipEmptyParts);

  if(language == QStringLiteral("python"))
  {
    return QStringLiteral(
               "False None True and as assert async await break class continue def del elif else "
               "except finally for from global if import in is lambda nonlocal not or pass raise "
               "return try while with yield")
        .split(QLatin1Char(' '), QString::SkipEmptyParts);
  }

  ret +=
      QStringLiteral(
          "Buffer ByteAddressBuffer StructuredBuffer RWBuffer RWByteAddressBuffer "
          "RWStructuredBuffer Texture1D Texture1DArray Texture2D Texture2DArray Texture2DMS "
          "Texture2DMSArray Texture3D TextureCube RWTexture1D RWTexture1DArray RWTexture2D "
          "RWTexture2DArray RWTexture3D SamplerState SamplerComparisonState InputPatch "
          "OutputPatch AppendStructuredBuffer ConsumeStructuredBuffer abs acos all any asin "
          "atan atan2 ceil clamp clip cos cross ddx ddy degrees determinant distance dot exp exp2 "
          "faceforward floor fma fmod frac frexp fwidth isfinite isinf isnan ldexp length lerp log "
          "log2 max min modf mul normalize pow radians rcp reflect refract round rsqrt saturate "
          "sign "
          "sin sincos smoothstep sqrt step tan transpose trunc texture textureLod textureGrad "
          "texelFetch imageLoad imageStore barrier memoryBarrier atomicAdd SV_Position SV_Target "
          "SV_VertexID SV_InstanceID SV_DispatchThreadID SV_GroupID SV_GroupIndex SV_GroupThreadID")
          .split(QLatin1Char(' '), QString::SkipEmptyParts);
  return ret;
}

QByteArray maskedCode(const QByteArray &text, const QString &language)
{
  QByteArray masked = text;
  enum State
  {
    Normal,
    LineComment,
    BlockComment,
    SingleString,
    DoubleString,
    TripleSingleString,
    TripleDoubleString,
  };
  State state = Normal;
  bool escaped = false;
  const bool python = language == QStringLiteral("python");

  for(int i = 0; i < text.size(); i++)
  {
    const char c = text[i];
    const char next = i + 1 < text.size() ? text[i + 1] : '\0';

    if(state == Normal)
    {
      if((python && c == '#') || (!python && c == '/' && next == '/'))
      {
        state = LineComment;
        masked[i] = ' ';
        if(!python)
          masked[++i] = ' ';
      }
      else if(!python && c == '/' && next == '*')
      {
        state = BlockComment;
        masked[i] = ' ';
        masked[++i] = ' ';
      }
      else if(python && c == '\'' && i + 2 < text.size() && text[i + 1] == '\'' && text[i + 2] == '\'')
      {
        state = TripleSingleString;
        masked[i] = masked[i + 1] = masked[i + 2] = ' ';
        i += 2;
      }
      else if(python && c == '"' && i + 2 < text.size() && text[i + 1] == '"' && text[i + 2] == '"')
      {
        state = TripleDoubleString;
        masked[i] = masked[i + 1] = masked[i + 2] = ' ';
        i += 2;
      }
      else if(c == '\'')
      {
        state = SingleString;
        escaped = false;
        masked[i] = ' ';
      }
      else if(c == '"')
      {
        state = DoubleString;
        escaped = false;
        masked[i] = ' ';
      }
    }
    else if(state == LineComment)
    {
      if(c == '\n')
        state = Normal;
      else
        masked[i] = ' ';
    }
    else if(state == BlockComment)
    {
      if(c == '*' && next == '/')
      {
        masked[i] = masked[i + 1] = ' ';
        i++;
        state = Normal;
      }
      else if(c != '\n')
      {
        masked[i] = ' ';
      }
    }
    else if(state == TripleSingleString || state == TripleDoubleString)
    {
      const char quote = state == TripleSingleString ? '\'' : '"';
      if(c == quote && i + 2 < text.size() && text[i + 1] == quote && text[i + 2] == quote)
      {
        masked[i] = masked[i + 1] = masked[i + 2] = ' ';
        i += 2;
        state = Normal;
      }
      else if(c != '\n')
      {
        masked[i] = ' ';
      }
    }
    else
    {
      const char quote = state == SingleString ? '\'' : '"';
      if(c != '\n')
        masked[i] = ' ';
      if(c == quote && !escaped)
        state = Normal;
      if(c == '\n' && !escaped)
        state = Normal;
      escaped = c == '\\' && !escaped;
      if(c != '\\')
        escaped = false;
    }
  }

  return masked;
}

QString cleanSignature(const QString &signature)
{
  QString ret = signature;
  ret.replace(QRegularExpression(QStringLiteral("\\s+")), QStringLiteral(" "));
  return ret.trimmed().left(512);
}

QString pathFromUri(const QString &uri)
{
  const QUrl url(uri);
  return url.isLocalFile() ? QDir::cleanPath(url.toLocalFile()) : QString();
}

QHash<int, int> codeDelimiterPairs(const QByteArray &code)
{
  QList<int> stack;
  QHash<int, int> pairs;
  for(int i = 0; i < code.size(); ++i)
  {
    if(code[i] == '{' || code[i] == '(' || code[i] == '[')
      stack.push_back(i);
    else if((code[i] == '}' || code[i] == ')' || code[i] == ']') && !stack.isEmpty())
    {
      const int open = stack.takeLast();
      if((code[open] == '{' && code[i] == '}') ||
         (code[open] == '(' && code[i] == ')') ||
         (code[open] == '[' && code[i] == ']'))
        pairs.insert(open, i);
    }
  }
  return pairs;
}

struct FollowingDeclarator
{
  QString name;
  QString valueType;
  int position = -1;
};

QList<FollowingDeclarator> followingDeclarators(const QByteArray &code,
                                               const QHash<int, int> &pairs, int position,
                                               QString baseType)
{
  QList<FollowingDeclarator> ret;
  // Pointer/reference modifiers belong to each declarator, not the common base type.
  while(baseType.endsWith(QLatin1Char('*')) || baseType.endsWith(QLatin1Char('&')))
    baseType = baseType.left(baseType.size() - 1).trimmed();
  auto skipSpace = [&code](int index) {
    while(index < code.size() && std::isspace(static_cast<unsigned char>(code[index]))) ++index;
    return index;
  };
  for(int i = position; i < code.size() && ret.size() < 256; ++i)
  {
    const char c = code[i];
    if(c == ';' || c == ')' || c == '}' || c == ']') break;
    if(c == '(' || c == '{' || c == '[')
    {
      if(!pairs.contains(i)) break;
      i = pairs.value(i); // Constructor arguments, array indices and aggregate initializers.
      continue;
    }
    if(c != ',') continue;
    int start = skipSpace(i + 1);
    QString valueType = baseType;
    while(start < code.size() && (code[start] == '*' || code[start] == '&'))
    {
      valueType += QChar::fromLatin1(code[start]);
      start = skipSpace(start + 1);
    }
    if(start >= code.size() || !isIdentifierStartByte(code[start])) break;
    int end = start + 1;
    while(end < code.size() && isIdentifierByte(code[end])) ++end;
    const int next = skipSpace(end);
    if(next >= code.size() ||
       (code[next] != '=' && code[next] != ';' && code[next] != ',' &&
        code[next] != ':' && code[next] != '[' && code[next] != ')'))
      break; // A new typed parameter (e.g. ', float other') is not another declarator.
    FollowingDeclarator declaration;
    declaration.name = QString::fromUtf8(code.mid(start, end - start));
    declaration.valueType = valueType;
    declaration.position = start;
    ret.push_back(declaration);
    i = end - 1;
  }
  return ret;
}
}    // namespace

struct CodeEditorService::Impl
{
  struct EditorState;

  struct Symbol
  {
    QString name;
    QString signature;
    EditorState *state = NULL;
    int position = -1;
    int scopeStart = 0;
    int scopeEnd = 0;
    QString ownerType;
    QString valueType;
  };

  struct EditorState
  {
    QPointer<ScintillaEdit> editor;
    QString fileName;
    QString uri;
    QString includeBaseDirectory;
    QString language;
    QByteArray text;
    QByteArray semanticText;
    QByteArray synchronizedSemanticText;
    bool relocatedMacroInclude = false;
    QString sourceSha256;
    QStringList includedFiles;
    QList<Symbol> symbols;
    QList<Diagnostic> localDiagnostics;
    QList<Diagnostic> lspDiagnostics;
    QList<Diagnostic> externalDiagnostics;
    int version = 1;
    quint64 editRevision = 1;
    bool textDirty = false;
    bool diagnosticsDirty = false;
    bool openSent = false;
    bool diagnosticTipActive = false;
    QByteArray highlightedWord;
    int highlightedVersion = -1;
    int coloredVersion = -1;
    QByteArray maskedText;
    int maskedVersion = -1;
    int semanticRequestedVersion = -1;
    quint64 semanticPaintGeneration = 0;
  };

  enum class PendingKind
  {
    Initialize,
    Completion,
    Definition,
    References,
    Highlights,
    ReferenceDefinition,
    ReferenceCandidate,
    SemanticTokens,
  };

  struct PendingRequest
  {
    PendingKind kind = PendingKind::Completion;
    QPointer<ScintillaEdit> editor;
    ScintillaEdit *sourceEditor = NULL;    // identity only; never dereferenced
    QString prefix;
    int version = 0;
    quint64 editRevision = 0;
    int position = -1;
    int referenceGeneration = 0;
    Location referenceCandidate;
    QPointer<ScintillaEdit> candidateEditor;
    QHash<ScintillaEdit *, quint64> documentRevisions;
  };

  struct ReferenceSearch
  {
    PendingRequest origin;
    Location definition;
    QString definitionKey;
    QString cacheKey;
    QList<Location> candidates;
    QList<Location> hits;
    int generation = 0;
    int outstanding = 0;
    bool truncated = false;
  };

  struct NavigationEntry
  {
    QPointer<ScintillaEdit> editor;
    QString fileName;
    int position = -1;
    int line = -1;
    int column = -1;
  };

  QPointer<QMenu> referencesMenu;
  bool deliveringNavigation = false;

  explicit Impl(CodeEditorService *q) : owner(q)
  {
    diagnosticTimer = new QTimer(owner);
    diagnosticTimer->setSingleShot(true);
    diagnosticTimer->setInterval(DiagnosticsDebounceMS);
    QObject::connect(diagnosticTimer, &QTimer::timeout, owner, [this]() { flushDirtyDocuments(); });

    semanticTimer = new QTimer(owner);
    semanticTimer->setSingleShot(true);
    semanticTimer->setInterval(SemanticTokensDebounceMS);
    QObject::connect(semanticTimer, &QTimer::timeout, owner, [this]() {
      flushDirtyDocuments();
      for(EditorState *state : states) requestSemanticTokens(state);
    });

    process = new QProcess(owner);
    ConfigureBackgroundProcess(process);
    process->setProcessChannelMode(QProcess::SeparateChannels);
    QObject::connect(process, &QProcess::started, owner, [this]() { sendInitialize(); });
    QObject::connect(process, &QProcess::readyReadStandardOutput, owner,
                     [this]() { readServerOutput(); });
    QObject::connect(process, &QProcess::readyReadStandardError, owner, [this]() {
      // Never forward unbounded server output into the UI. Keep only the last useful line.
      QByteArray error = process->readAllStandardError();
      if(error.size() > 4096)
        error = error.right(4096);
      const QString message = QString::fromUtf8(error).trimmed();
      if(!message.isEmpty())
        lastServerError = message.right(1024);
    });
    QObject::connect(
        process, static_cast<void (QProcess::*)(int, QProcess::ExitStatus)>(&QProcess::finished),
        owner, [this](int exitCode, QProcess::ExitStatus status) {
          const bool unexpected = !stopping;
          serverReady = false;
          semanticTokenTypes.clear();
          semanticTokensProvider = false;
          pending.clear();
          queuedNavigation.clear();
          referenceSearches.clear();
          receiveBuffer.clear();
          for(EditorState *state : states)
          {
            state->openSent = false;
            state->semanticRequestedVersion = -1;
          }
          if(unexpected)
          {
            QString message;
            if(status == QProcess::CrashExit)
            {
              message = lastServerError.isEmpty()
                            ? QCoreApplication::translate(
                                  "CodeEditorService",
                                  "Shader language server crashed (exit %1).")
                                  .arg(exitCode)
                            : QCoreApplication::translate(
                                  "CodeEditorService",
                                  "Shader language server crashed (exit %1): %2")
                                  .arg(exitCode)
                                  .arg(lastServerError);
            }
            else
            {
              message = lastServerError.isEmpty()
                            ? QCoreApplication::translate(
                                  "CodeEditorService",
                                  "Shader language server stopped (exit %1).")
                                  .arg(exitCode)
                            : QCoreApplication::translate(
                                  "CodeEditorService",
                                  "Shader language server stopped (exit %1): %2")
                                  .arg(exitCode)
                                  .arg(lastServerError);
            }
            reportStatus(message);
          }
          stopping = false;
        });
    QObject::connect(process, &QProcess::errorOccurred, owner, [this](QProcess::ProcessError) {
      queuedNavigation.clear();
      referenceSearches.clear();
      if(!stopping)
        reportStatus(
            QCoreApplication::translate("CodeEditorService",
                                        "Shader language server could not start: %1")
                .arg(process->errorString()));
    });
  }

  ~Impl()
  {
    diagnosticTimer->stop();
    semanticTimer->stop();
    stopping = true;
    if(process->state() != QProcess::NotRunning)
    {
      process->terminate();
      if(!process->waitForFinished(150))
      {
        process->kill();
        process->waitForFinished(150);
      }
    }

    for(EditorState *state : states)
      delete state;
    states.clear();
  }

  CodeEditorService *owner = NULL;
  QTimer *diagnosticTimer = NULL;
  QTimer *semanticTimer = NULL;
  QHash<ScintillaEdit *, EditorState *> states;
  QHash<QString, QList<Symbol>> symbolIndex;
  QList<NavigationEntry> backHistory;
  QList<NavigationEntry> forwardHistory;
  DefinitionHandler definitionHandler;
  DiagnosticsHandler diagnosticsHandler;
  StatusHandler statusHandler;
  quint64 untitledCounter = 0;

  QProcess *process = NULL;
  QString configuredProgram;
  QString resolvedProgram;
  QStringList serverArguments;
  QByteArray receiveBuffer;
  QHash<int, PendingRequest> pending;
  int nextRequestId = 1;
  bool serverReady = false;
  int textDocumentSyncKind = 1;
  bool definitionProvider = false;
  bool referencesProvider = false;
  bool highlightProvider = false;
  bool semanticTokensProvider = false;
  QStringList semanticTokenTypes;
  quint64 semanticPaintSerial = 0;
  QHash<ScintillaEdit *, PendingRequest> queuedNavigation;
  QHash<ScintillaEdit *, ReferenceSearch> referenceSearches;
  QHash<QString, QList<Location>> referenceCache;
  int nextReferenceGeneration = 1;
  bool serverAttempted = false;
  bool stopping = false;
  QString lastServerError;

  void reportStatus(const QString &message)
  {
    if(statusHandler)
      statusHandler(message);
  }

  EditorState *stateFor(ScintillaEdit *editor) const { return states.value(editor, NULL); }

  bool supportsSemanticLanguage(EditorState *state) const
  {
    return state && (state->language == QStringLiteral("hlsl") ||
                     state->language == QStringLiteral("glsl") ||
                     state->language == QStringLiteral("slang"));
  }

  EditorState *stateForUri(const QString &uri) const
  {
    for(EditorState *state : states)
      if(state->uri == uri)
        return state;

    const QString path = pathFromUri(uri);
    if(path.isEmpty())
      return NULL;

    for(EditorState *state : states)
    {
      const QString semanticPath = pathFromUri(state->uri);
      if(!semanticPath.isEmpty() &&
         QFileInfo(semanticPath).absoluteFilePath().compare(path, Qt::CaseInsensitive) == 0)
        return state;
    }
    return NULL;
  }

  QString makeUri(const QString &fileName)
  {
    if(!fileName.trimmed().isEmpty() && !fileName.startsWith(QLatin1Char('<')))
      return QUrl::fromLocalFile(QFileInfo(fileName).absoluteFilePath()).toString(QUrl::FullyEncoded);

    return QStringLiteral("untitled:renderdoc/%1/shader").arg(++untitledCounter);
  }

  void configureIndicators(ScintillaEdit *editor)
  {
    editor->indicSetStyle(ErrorIndicator, INDIC_SQUIGGLE);
    editor->indicSetFore(ErrorIndicator, scintillaColour(255, 86, 71));
    editor->indicSetUnder(ErrorIndicator, true);
    editor->indicSetStyle(WarningIndicator, INDIC_SQUIGGLELOW);
    editor->indicSetFore(WarningIndicator, scintillaColour(245, 216, 106));
    editor->indicSetUnder(WarningIndicator, true);
    editor->indicSetStyle(SymbolOccurrenceIndicator, INDIC_ROUNDBOX);
    editor->indicSetFore(SymbolOccurrenceIndicator, scintillaColour(100, 150, 210));
    editor->indicSetAlpha(SymbolOccurrenceIndicator, 45);
    editor->indicSetOutlineAlpha(SymbolOccurrenceIndicator, 150);
    editor->indicSetUnder(SymbolOccurrenceIndicator, true);
    const bool dark = editor->palette().color(QPalette::Base).lightness() < 128;
    editor->indicSetStyle(FunctionNameIndicator, INDIC_TEXTFORE);
    editor->indicSetFore(FunctionNameIndicator, dark ? scintillaColour(57, 204, 155)
                                                    : scintillaColour(0, 112, 82));
    editor->indicSetStyle(VariableNameIndicator, INDIC_TEXTFORE);
    editor->indicSetFore(VariableNameIndicator, dark ? scintillaColour(156, 205, 254)
                                                    : scintillaColour(38, 78, 130));
    editor->indicSetStyle(SemanticNameIndicator, INDIC_TEXTFORE);
    editor->indicSetFlags(SemanticNameIndicator, SC_INDICFLAG_VALUEFORE);
    editor->autoCSetSeparator('\n');
    editor->autoCSetIgnoreCase(true);
    editor->autoCSetAutoHide(true);
    editor->autoCSetDropRestOfWord(false);
    editor->autoCSetMaxHeight(12);
    editor->autoCSetMaxWidth(64);
    editor->setMouseDwellTime(500);
    // Diagnostic hover tips are Scintilla call tips. Their default colours
    // (white background, inherited light foreground) are unreadable on the
    // dark theme, so the tip looked empty. Give them explicit theme colours.
    editor->callTipUseStyle(0);
    editor->callTipSetBack(dark ? scintillaColour(45, 47, 52) : scintillaColour(255, 255, 240));
    editor->callTipSetFore(dark ? scintillaColour(224, 226, 230) : scintillaColour(30, 30, 30));
    editor->callTipSetForeHlt(dark ? scintillaColour(120, 190, 255) : scintillaColour(20, 90, 200));
  }

  void addSymbol(EditorState *state, const QString &name, int position,
                 const QString &signature = QString(), const QString &valueType = QString())
  {
    if(name.isEmpty() || position < 0 || name.size() > 128)
      return;

    for(const Symbol &existing : state->symbols)
      if(existing.name == name && existing.position == position)
        return;

    Symbol symbol;
    symbol.name = name;
    symbol.position = position;
    symbol.signature = signature;
    symbol.state = state;
    symbol.scopeEnd = state->text.size();
    symbol.valueType = valueType;
    state->symbols.push_back(symbol);
  }

  void indexDocument(EditorState *state)
  {
    state->symbols.clear();
    const QByteArray masked = maskedCode(state->text, state->language);
    const QString source = QString::fromUtf8(masked);
    state->includedFiles.clear();
    // An open tab is not a translation unit dependency. Only explicit includes may extend
    // the local navigation scope into another document (many shaders use identical names).
    static const QRegularExpression includeDirective(
        QStringLiteral("(?m)^\\s*#\\s*include\\s*[\"<]([^\">\\r\\n]+)[\">]"));
    const QString original = QString::fromUtf8(state->text);
    QString projected;
    int copiedThrough = 0;
    state->relocatedMacroInclude = false;
    if(!state->includeBaseDirectory.isEmpty())
    {
      static const QRegularExpression macroInclude(
          QStringLiteral("(?m)^[ \\t]*#[ \\t]*include[ \\t]+([^\"< \\t\\r\\n][^\\r\\n]*)"));
      auto macros = macroInclude.globalMatch(original);
      while(macros.hasNext())
      {
        const auto match = macros.next();
        const int hash = original.indexOf(QLatin1Char('#'), match.capturedStart());
        const int byte = utf16IndexToByte(original, hash);
        if(byte >= 0 && byte < masked.size() && masked[byte] == '#')
          state->relocatedMacroInclude = true;
      }
    }
    auto includes = includeDirective.globalMatch(original);
    while(includes.hasNext())
    {
      const auto match = includes.next();
      const int hash = original.indexOf(QLatin1Char('#'), match.capturedStart());
      const int byte = utf16IndexToByte(original, hash);
      if(byte >= 0 && byte < masked.size() && masked[byte] == '#')
      {
        state->includedFiles.push_back(QDir::cleanPath(
            (state->includeBaseDirectory.isEmpty() ? QFileInfo(state->fileName).absoluteDir()
                                                  : QDir(state->includeBaseDirectory))
                .absoluteFilePath(match.captured(1))));
        if(!state->includeBaseDirectory.isEmpty() && QDir::isRelativePath(match.captured(1)))
        {
          // Keep the existing preview URI and all source line numbers. Slang requires an
          // existing file for own-file definition locations, so fictitious preview URIs
          // break navigation. Only the LSP copy receives absolute literal include paths.
          const int start = match.capturedStart(1);
          projected += original.mid(copiedThrough, start - copiedThrough);
          projected += QDir::fromNativeSeparators(state->includedFiles.back());
          copiedThrough = start + match.capturedLength(1);
        }
      }
    }
    state->semanticText = copiedThrough ? (projected + original.mid(copiedThrough)).toUtf8()
                                       : state->text;

    if(state->language == QStringLiteral("python"))
    {
      static const QRegularExpression pythonDefinition(
          QStringLiteral("\\b(?:def|class)\\s+([A-Za-z_][A-Za-z0-9_]*)\\s*(\\([^\\n]*\\))?"));
      QRegularExpressionMatchIterator matches = pythonDefinition.globalMatch(source);
      while(matches.hasNext())
      {
        const QRegularExpressionMatch match = matches.next();
        const QString name = match.captured(1);
        const int position = utf16IndexToByte(source, match.capturedStart(1));
        addSymbol(state, name, position, cleanSignature(match.captured(0)));
      }
      return;
    }

    static const QRegularExpression typeDefinition(QStringLiteral(
        "\\b(?:struct|class|interface|cbuffer|namespace|enum)\\s+([A-Za-z_][A-Za-z0-9_]*)"));
    static const QRegularExpression usingDefinition(
        QStringLiteral("\\busing\\s+([A-Za-z_][A-Za-z0-9_]*)\\s*="));
    static const QRegularExpression functionDefinition(
        QStringLiteral("\\b([A-Za-z_][A-Za-z0-9_:<>]*(?:\\s*[*&])?)\\s+"
                       "([A-Za-z_][A-Za-z0-9_]*)\\s*(\\([^;{}]*\\))\\s*(?=[:{;])"));
    static const QRegularExpression variableDefinition(
        QStringLiteral("\\b([A-Za-z_][A-Za-z0-9_:<>]*(?:\\s*[*&])?)\\s+"
                       "([A-Za-z_][A-Za-z0-9_]*)\\s*(?=[=;,:\\[)])"));
    static const QSet<QString> invalidTypes = {
        QStringLiteral("return"), QStringLiteral("if"),      QStringLiteral("for"),
        QStringLiteral("while"),  QStringLiteral("switch"),  QStringLiteral("case"),
        QStringLiteral("sizeof"), QStringLiteral("discard"), QStringLiteral("else"),
    };

    auto addMatches = [this, state, &source](const QRegularExpression &expression, int nameGroup,
                                             bool function) {
      QRegularExpressionMatchIterator matches = expression.globalMatch(source);
      while(matches.hasNext())
      {
        const QRegularExpressionMatch match = matches.next();
        if(match.capturedStart(nameGroup) < 0)
          continue;
        if(nameGroup > 1 && invalidTypes.contains(match.captured(1)))
          continue;
        const QString name = match.captured(nameGroup);
        const int position = utf16IndexToByte(source, match.capturedStart(nameGroup));
        addSymbol(state, name, position, function ? cleanSignature(match.captured(0)) : QString(),
                  nameGroup == 2 ? match.captured(1).trimmed() : QString());
      }
    };

    addMatches(typeDefinition, 1, false);
    addMatches(usingDefinition, 1, false);
    addMatches(functionDefinition, 2, true);
    addMatches(variableDefinition, 2, false);

    // Brace scopes and parameter ranges distinguish local shadowing and fields belonging to
    // different structs. This is a conservative local fallback; macros and overload typing
    // remain the language server/compiler's responsibility.
    struct Scope { int start; int end; QString ownerType; bool cbuffer; };
    QList<Scope> scopes;
    const QHash<int, int> pairs = codeDelimiterPairs(masked);
    const QList<Symbol> firstDeclarators = state->symbols;
    for(const Symbol &symbol : firstDeclarators)
    {
      if(!symbol.signature.isEmpty() || symbol.valueType.isEmpty()) continue;
      for(const FollowingDeclarator &declaration : followingDeclarators(
              masked, pairs, symbol.position + symbol.name.toUtf8().size(), symbol.valueType))
        addSymbol(state, declaration.name, declaration.position, QString(), declaration.valueType);
    }
    static const QRegularExpression typeScope(
        QStringLiteral("\\b(struct|class|cbuffer)\\s+([A-Za-z_][A-Za-z0-9_]*)\\s*(?::[^{};]*)?$"));
    for(auto it = pairs.constBegin(); it != pairs.constEnd(); ++it)
    {
      if(masked[it.key()] != '{') continue;
      const int start = qMax(0, qMax(masked.lastIndexOf(';', it.key()),
                                   qMax(masked.lastIndexOf('}', it.key()),
                                        masked.lastIndexOf('{', it.key() - 1))) + 1);
      const auto match = typeScope.match(QString::fromUtf8(masked.mid(start, it.key() - start)));
      scopes.push_back({it.key(), it.value(), match.captured(2),
                        match.captured(1) == QStringLiteral("cbuffer")});
    }
    auto skipSpace = [&masked](int index) {
      while(index < masked.size() && std::isspace(static_cast<unsigned char>(masked[index]))) ++index;
      return index;
    };
    std::function<int(int, int)> statementEnd = [&](int index, int depth) -> int {
      index = skipSpace(index);
      if(index >= masked.size() || depth > 32) return index;
      if(masked[index] == '{') return pairs.value(index, index) + 1;
      for(const QByteArray &control : {QByteArray("if"), QByteArray("for"), QByteArray("while"), QByteArray("switch")})
      {
        if(masked.mid(index, control.size()) != control) continue;
        int open = skipSpace(index + control.size());
        if(open >= masked.size() || masked[open] != '(' || !pairs.contains(open)) continue;
        int end = statementEnd(pairs.value(open) + 1, depth + 1);
        int next = skipSpace(end);
        if(control == "if" && masked.mid(next, 4) == "else" &&
           (next + 4 >= masked.size() || !isIdentifierByte(masked[next + 4])))
          end = statementEnd(next + 4, depth + 1);
        return end;
      }
      for(int i = index; i < masked.size(); ++i)
      {
        if(masked[i] == ';') return i + 1;
        if(masked[i] == '}') return i;
        if(pairs.contains(i)) i = pairs.value(i);
      }
      return masked.size();
    };
    QList<QPair<int, int>> forScopes;
    for(int offset = 0; (offset = masked.indexOf("for", offset)) >= 0; offset += 3)
    {
      if(offset > 0 && isIdentifierByte(masked[offset - 1])) continue;
      const int open = skipSpace(offset + 3);
      if(open < masked.size() && masked[open] == '(' && pairs.contains(open))
        forScopes.push_back(qMakePair(open, statementEnd(pairs.value(open) + 1, 0)));
    }
    for(Symbol &symbol : state->symbols)
    {
      for(const Scope &scope : scopes)
      {
        if(scope.start < symbol.position && symbol.position < scope.end &&
           scope.end - scope.start < symbol.scopeEnd - symbol.scopeStart)
        {
          if(scope.cbuffer) continue; // CBuffer members are visible as globals in HLSL.
          symbol.scopeStart = scope.start;
          symbol.scopeEnd = scope.end;
          symbol.ownerType = scope.ownerType;
        }
      }
      if(!symbol.signature.isEmpty()) continue;
      for(const Symbol &function : state->symbols)
      {
        if(function.signature.isEmpty()) continue;
        int open = masked.indexOf('(', function.position);
        int close = pairs.value(open, -1);
        if(!(open < symbol.position && symbol.position < close)) continue;
        // Prototype parameter names have no visibility outside their own declaration.
        symbol.scopeStart = open;
        symbol.scopeEnd = close;
        int body = masked.indexOf('{', close);
        int terminator = masked.indexOf(';', close);
        if(body >= 0 && (terminator < 0 || body < terminator) && pairs.contains(body))
        {
          symbol.scopeStart = open;
          symbol.scopeEnd = pairs.value(body);
        }
      }
      for(const auto &scope : forScopes)
        if(scope.first < symbol.position && symbol.position < pairs.value(scope.first))
        {
          symbol.scopeStart = scope.first;
          symbol.scopeEnd = scope.second;
        }
    }
  }

  void removeIndexedDocument(EditorState *state)
  {
    QSet<QString> names;
    for(const Symbol &symbol : state->symbols)
      names.insert(symbol.name);
    for(const QString &name : names)
    {
      auto found = symbolIndex.find(name);
      if(found == symbolIndex.end()) continue;
      QList<Symbol> &symbols = found.value();
      for(int i = symbols.size() - 1; i >= 0; --i)
        if(symbols[i].state == state) symbols.removeAt(i);
      if(symbols.isEmpty()) symbolIndex.erase(found);
    }
  }

  void reindexDocument(EditorState *state)
  {
    removeIndexedDocument(state);
    indexDocument(state);
    for(const Symbol &symbol : state->symbols)
      symbolIndex[symbol.name].push_back(symbol);
  }

  QList<Diagnostic> localDiagnosticsFor(EditorState *state) const
  {
    struct Delimiter
    {
      char character = 0;
      int position = 0;
    };

    QList<Diagnostic> ret;
    QVector<Delimiter> stack;
    const QByteArray &text = state->text;
    const bool python = state->language == QStringLiteral("python");
    enum ScanState
    {
      Normal,
      LineComment,
      BlockComment,
      SingleString,
      DoubleString,
      TripleSingleString,
      TripleDoubleString,
    };
    ScanState scan = Normal;
    int constructStart = -1;
    bool escaped = false;

    auto append = [&ret, &text](int start, int length, DiagnosticSeverity severity,
                                const QString &message) {
      if(ret.size() >= MaxDiagnostics)
        return;
      Diagnostic diagnostic;
      diagnostic.start = qBound(0, start, text.size());
      diagnostic.length = qMax(1, qMin(length, text.size() - diagnostic.start));
      diagnostic.severity = severity;
      diagnostic.message = message;
      // i18n-ignore: Diagnostic source is internal metadata and is not shown by the current UI.
      diagnostic.source = QStringLiteral("RenderDoc local parser");
      const QByteArray prefix = text.left(diagnostic.start);
      diagnostic.line = prefix.count('\n');
      const int newline = prefix.lastIndexOf('\n');
      diagnostic.column = diagnostic.start - (newline + 1);
      ret.push_back(diagnostic);
    };

    for(int i = 0; i < text.size(); i++)
    {
      const char c = text[i];
      const char next = i + 1 < text.size() ? text[i + 1] : '\0';

      if(scan == Normal)
      {
        if((python && c == '#') || (!python && c == '/' && next == '/'))
        {
          scan = LineComment;
          if(!python)
            i++;
        }
        else if(!python && c == '/' && next == '*')
        {
          scan = BlockComment;
          constructStart = i++;
        }
        else if(python && c == '\'' && i + 2 < text.size() && text[i + 1] == '\'' &&
                text[i + 2] == '\'')
        {
          scan = TripleSingleString;
          constructStart = i;
          i += 2;
        }
        else if(python && c == '"' && i + 2 < text.size() && text[i + 1] == '"' && text[i + 2] == '"')
        {
          scan = TripleDoubleString;
          constructStart = i;
          i += 2;
        }
        else if(c == '\'' || c == '"')
        {
          scan = c == '\'' ? SingleString : DoubleString;
          constructStart = i;
          escaped = false;
        }
        else if(c == '(' || c == '[' || c == '{')
        {
          stack.push_back({c, i});
        }
        else if(c == ')' || c == ']' || c == '}')
        {
          const char expected = c == ')' ? '(' : (c == ']' ? '[' : '{');
          if(stack.isEmpty() || stack.back().character != expected)
          {
            append(i, 1, DiagnosticSeverity::Error,
                   QCoreApplication::translate("CodeEditorService", "Unmatched closing '%1'")
                       .arg(QChar::fromLatin1(c)));
          }
          else
          {
            stack.pop_back();
          }
        }
      }
      else if(scan == LineComment)
      {
        if(c == '\n')
          scan = Normal;
      }
      else if(scan == BlockComment)
      {
        if(c == '*' && next == '/')
        {
          scan = Normal;
          i++;
        }
      }
      else if(scan == TripleSingleString || scan == TripleDoubleString)
      {
        const char quote = scan == TripleSingleString ? '\'' : '"';
        if(c == quote && i + 2 < text.size() && text[i + 1] == quote && text[i + 2] == quote)
        {
          scan = Normal;
          i += 2;
        }
      }
      else
      {
        const char quote = scan == SingleString ? '\'' : '"';
        if(c == quote && !escaped)
        {
          scan = Normal;
        }
        else if(c == '\n' && !escaped)
        {
          append(constructStart, qMax(1, i - constructStart), DiagnosticSeverity::Error,
                 QCoreApplication::translate("CodeEditorService",
                                             "Unterminated string literal"));
          scan = Normal;
        }
        escaped = c == '\\' && !escaped;
        if(c != '\\')
          escaped = false;
      }
    }

    if(scan == BlockComment)
      append(constructStart, qMax(1, text.size() - constructStart), DiagnosticSeverity::Warning,
             QCoreApplication::translate("CodeEditorService", "Unterminated block comment"));
    else if(scan == SingleString || scan == DoubleString || scan == TripleSingleString ||
            scan == TripleDoubleString)
      append(constructStart, qMax(1, text.size() - constructStart), DiagnosticSeverity::Error,
             QCoreApplication::translate("CodeEditorService", "Unterminated string literal"));

    for(const Delimiter &delimiter : stack)
      append(delimiter.position, 1, DiagnosticSeverity::Error,
             QCoreApplication::translate("CodeEditorService", "Unclosed '%1'")
                 .arg(QChar::fromLatin1(delimiter.character)));

    return ret;
  }

  QList<Diagnostic> combinedDiagnostics(EditorState *state) const
  {
    QList<Diagnostic> ret = state->localDiagnostics;
    // Slang's semantic service is useful for navigation/coloring in GLSL, but its
    // GLSL dialect does not implement captured-program features such as member
    // row_major layouts, separate samplerless textures and EXT_spirv_intrinsics.
    // Those reports are not target-compiler errors. Keep local structural checks
    // and actual compiler diagnostics; other providers and HLSL remain unchanged.
    const bool partialGLSLProvider =
        state->language == QStringLiteral("glsl") &&
        QFileInfo(process->program()).completeBaseName().compare(QStringLiteral("slangd"),
                                                                Qt::CaseInsensitive) == 0;
    if(!partialGLSLProvider)
      ret.append(state->lspDiagnostics);
    ret.append(state->externalDiagnostics);
    return ret;
  }

  void displayDiagnostics(EditorState *state)
  {
    ScintillaEdit *editor = state->editor.data();
    if(!editor)
      return;

    const sptr_t previousIndicator = editor->indicatorCurrent();
    editor->setIndicatorCurrent(ErrorIndicator);
    editor->indicatorClearRange(0, editor->length());
    editor->setIndicatorCurrent(WarningIndicator);
    editor->indicatorClearRange(0, editor->length());

    const QList<Diagnostic> all = combinedDiagnostics(state);
    for(const Diagnostic &diagnostic : all)
    {
      editor->setIndicatorCurrent(
          diagnostic.severity == DiagnosticSeverity::Error ? ErrorIndicator : WarningIndicator);
      editor->indicatorFillRange(
          qBound(0, diagnostic.start, int(editor->length())),
          qMax(1, qMin(diagnostic.length, int(editor->length()) - diagnostic.start)));
    }
    editor->setIndicatorCurrent(previousIndicator);

    if(diagnosticsHandler)
      diagnosticsHandler(editor, all);
  }

  void syncText(EditorState *state, bool runDiagnostics)
  {
    if(!state || !state->editor)
      return;

    if(state->textDirty)
    {
      const QByteArray newText = state->editor->getText(state->editor->textLength() + 1);
      if(newText != state->text)
      {
        state->text = newText;
        state->sourceSha256 = QString::fromLatin1(
            QCryptographicHash::hash(state->text, QCryptographicHash::Sha256).toHex());
        state->version++;
        reindexDocument(state);
        sendDidChange(state);
      }
      state->textDirty = false;
    }

    if(runDiagnostics && state->diagnosticsDirty)
    {
      state->localDiagnostics = localDiagnosticsFor(state);
      state->diagnosticsDirty = false;
      displayDiagnostics(state);
    }
    updateSymbolHighlights(state);
  }

  void flushDirtyDocuments()
  {
    for(EditorState *state : states)
      if(state->textDirty || state->diagnosticsDirty)
        syncText(state, true);
  }

  void markModified(EditorState *state)
  {
    if(!state)
      return;
    cancelRequests(state->editor.data(), true);
    state->editRevision++;
    state->textDirty = true;
    state->diagnosticsDirty = true;
    state->lspDiagnostics.clear();
    // Compiler diagnostics supplied by the owning workspace describe the previous source
    // revision. Drop their locations as soon as the buffer changes instead of leaving stale
    // squiggles attached to unrelated text.
    state->externalDiagnostics.clear();
    for(EditorState *document : states)
    {
      if(document != state && !documentInScope(document, state->fileName, state)) continue;
      document->semanticRequestedVersion = -1;
      document->semanticPaintGeneration = ++semanticPaintSerial;
      if(document->editor)
      {
        const int previous = int(document->editor->indicatorCurrent());
        document->editor->setIndicatorCurrent(SemanticNameIndicator);
        document->editor->indicatorClearRange(0, document->editor->length());
        document->editor->setIndicatorCurrent(previous);
      }
    }
    if(referencesMenu)
      referencesMenu->close();
    diagnosticTimer->start();
    semanticTimer->start();
  }

  void moveHistoryForEdit(EditorState *state, int type, int position, int length)
  {
    auto update = [=](QList<NavigationEntry> &history) {
      for(NavigationEntry &entry : history)
      {
        if(entry.editor != state->editor || entry.position < position) continue;
        if(type & SC_MOD_INSERTTEXT) entry.position += length;
        else if(type & SC_MOD_DELETETEXT)
          entry.position = qMax(position, entry.position - length);
        entry.line = int(state->editor->lineFromPosition(entry.position));
        entry.column = entry.position - int(state->editor->positionFromLine(entry.line));
      }
    };
    update(backHistory);
    update(forwardHistory);
  }

  Location localDefinition(EditorState *sourceState, int position)
  {
    Location ret;
    if(!sourceState || !sourceState->editor)
      return ret;

    for(EditorState *state : states)
      if(state->textDirty) syncText(state, false);
    if(position < 0)
      position = int(sourceState->editor->currentPos());
    QPair<int, int> wordRange;
    const QString word = identifierAt(sourceState->text, position, &wordRange);
    if(word.isEmpty())
      return ret;

    return resolveDefinition(sourceState, wordRange.first, word);
  }

  Location resolveDefinition(EditorState *sourceState, int wordPosition, const QString &word,
                             int depth = 0, const QByteArray *maskedSource = NULL)
  {
    Location ret;
    if(depth > 8) return ret;
    const QList<Symbol> candidates = symbolIndex.value(word);
    const QByteArray masked = maskedSource ? *maskedSource
                                          : maskedCode(sourceState->text, sourceState->language);
    int previous = wordPosition - 1;
    while(previous >= 0 && isspace(static_cast<unsigned char>(masked[previous]))) --previous;
    const bool member = previous >= 0 && masked[previous] == '.';
    QString memberType;
    if(member)
    {
      --previous;
      while(previous >= 0 && isspace(static_cast<unsigned char>(masked[previous]))) --previous;
      // Resolve the receiver of an indexed field (lights[index].color), including nested
      // indices. The array subscript does not change its declared element type.
      while(previous >= 0 && masked[previous] == ']')
      {
        int nesting = 1;
        while(--previous >= 0 && nesting > 0)
        {
          if(masked[previous] == ']') ++nesting;
          else if(masked[previous] == '[') --nesting;
        }
        while(previous >= 0 && isspace(static_cast<unsigned char>(masked[previous]))) --previous;
      }
      QPair<int, int> range;
      const QString object = identifierAt(masked, previous, &range);
      const Location ownerLocation =
          resolveDefinition(sourceState, range.first, object, depth + 1, maskedSource);
      for(const Symbol &ownerSymbol : symbolIndex.value(object))
        if(ownerSymbol.state && ownerSymbol.state->editor == ownerLocation.editor &&
           ownerSymbol.position == ownerLocation.position)
          memberType = ownerSymbol.valueType;
      if(memberType.isEmpty()) return ret;
      static const QRegularExpression vectorType(
          QStringLiteral("^(?:float|half|double|int|uint|bool|min16float|min16int|min16uint)[1-4]$"));
      static const QRegularExpression swizzle(QStringLiteral("^(?:[xyzw]{1,4}|[rgba]{1,4})$"));
      if(vectorType.match(memberType).hasMatch() && swizzle.match(word).hasMatch())
        return ownerLocation;
    }
    const Symbol *best = NULL;
    int visibleFunctions = 0;
    qint64 bestScore = std::numeric_limits<qint64>::min();
    for(const Symbol &candidate : candidates)
    {
      if(!candidate.state || !candidate.state->editor)
        continue;
      if(!documentInScope(sourceState, candidate.state->fileName, candidate.state))
        continue;
      const bool atDeclaration = candidate.state == sourceState && candidate.position == wordPosition;
      if(!candidate.signature.isEmpty() && !atDeclaration) ++visibleFunctions;
      if(!atDeclaration)
      {
        if(member && candidate.ownerType != memberType) continue;
        if(!member && !candidate.ownerType.isEmpty()) continue;
        if(candidate.ownerType.isEmpty() && candidate.scopeStart > 0 &&
           (candidate.state != sourceState || wordPosition < candidate.position ||
            wordPosition > candidate.scopeEnd)) continue;
      }
      qint64 score = 0;
      if(candidate.state == sourceState)
      {
        score += 1000000000LL;
        if(candidate.position <= wordPosition)
          score += 500000000LL - qMin(500000000, wordPosition - candidate.position);
        else
          score -= candidate.position - wordPosition;
      }
      if(!best || score > bestScore)
      {
        best = &candidate;
        bestScore = score;
      }
    }

    if(!best)
      return ret;
    // The fallback lexer cannot select an overload by argument types. Let the compiler
    // provider resolve it instead of navigating to whichever declaration is nearest.
    if(!best->signature.isEmpty() && visibleFunctions > 1 && best->position != wordPosition)
      return ret;

    ret.editor = best->state->editor.data();
    ret.fileName = best->state->fileName;
    ret.position = best->position;
    ret.line = int(ret.editor->lineFromPosition(ret.position));
    ret.column = ret.position - int(ret.editor->positionFromLine(ret.line));
    return ret;
  }

  bool documentInScope(EditorState *source, const QString &targetPath,
                       EditorState *target = NULL) const
  {
    if(source == target) return true;
    if(!source || targetPath.isEmpty()) return false;
    const QString targetKey = QDir::cleanPath(QFileInfo(targetPath).absoluteFilePath());
    QList<EditorState *> pendingDocuments = {source};
    QSet<EditorState *> visited;
    while(!pendingDocuments.isEmpty())
    {
      EditorState *document = pendingDocuments.takeLast();
      if(visited.contains(document)) continue;
      visited.insert(document);
      for(const QString &included : document->includedFiles)
      {
        if(included.compare(targetKey, Qt::CaseInsensitive) == 0) return true;
        for(EditorState *open : states)
          if(!visited.contains(open) &&
             QDir::cleanPath(QFileInfo(open->fileName).absoluteFilePath())
                     .compare(included, Qt::CaseInsensitive) == 0)
            pendingDocuments.push_back(open);
      }
    }
    return false;
  }

  void deliverDefinition(const Location &location)
  {
    if(!location.editor && location.fileName.isEmpty())
      return;
    const bool wasDelivering = deliveringNavigation;
    deliveringNavigation = true;
    if(definitionHandler)
    {
      definitionHandler(location);
    }
    else if(location.editor && location.position >= 0)
    {
      location.editor->gotoPos(location.position);
      location.editor->QWidget::setFocus(Qt::ShortcutFocusReason);
    }
    deliveringNavigation = wasDelivering;
  }

  QByteArray indentationUnit(ScintillaEdit *editor) const
  {
    if(editor->useTabs())
      return QByteArray("\t");
    int width = int(editor->indent());
    if(width <= 0)
      width = int(editor->tabWidth());
    return QByteArray(qMax(1, width), ' ');
  }

  QByteArray lineIndentationBytes(ScintillaEdit *editor, int line) const
  {
    const int start = int(editor->positionFromLine(line));
    const int end = int(editor->lineIndentPosition(line));
    return editor->textRange(start, end);
  }

  QPair<int, int> selectedLineRange(ScintillaEdit *editor) const
  {
    const int start = int(editor->selectionStart());
    const int end = int(editor->selectionEnd());
    int firstLine = int(editor->lineFromPosition(start));
    int lastLine = int(editor->lineFromPosition(end));
    if(end > start && end == editor->positionFromLine(lastLine))
      lastLine--;
    return qMakePair(firstLine, qMax(firstLine, lastLine));
  }

  bool insertSmartNewline(EditorState *state)
  {
    ScintillaEdit *editor = state ? state->editor.data() : NULL;
    if(!editor || editor->readOnly() || editor->selections() != 1 ||
       editor->selectionIsRectangle())
      return false;

    const int start = int(editor->selectionStart());
    const int end = int(editor->selectionEnd());
    const QByteArray text = editor->getText(editor->textLength() + 1);
    const SmartNewlineEdit edit =
        planSmartNewline(text, start, end, eolBytes(int(editor->eOLMode())),
                         indentationUnit(editor), state->language == QStringLiteral("python"));

    editor->beginUndoAction();
    editor->setTargetRange(start, end);
    editor->replaceTarget(edit.replacement.size(), edit.replacement.constData());
    editor->setEmptySelection(start + edit.caretOffset);
    editor->endUndoAction();
    return true;
  }

  bool insertOpenLine(EditorState *state, bool above)
  {
    ScintillaEdit *editor = state ? state->editor.data() : NULL;
    if(!editor || editor->readOnly() || editor->selections() != 1 ||
       editor->selectionIsRectangle())
      return false;

    const int line = int(editor->lineFromPosition(editor->currentPos()));
    const QByteArray indentation = lineIndentationBytes(editor, line);
    const QByteArray eol = eolBytes(int(editor->eOLMode()));
    const int insertionPosition =
        above ? int(editor->positionFromLine(line)) : int(editor->lineEndPosition(line));
    const QByteArray insertion = above ? indentation + eol : eol + indentation;
    const int caret = insertionPosition + (above ? indentation.size() : insertion.size());

    editor->beginUndoAction();
    editor->insertText(insertionPosition, insertion.constData());
    editor->setEmptySelection(caret);
    editor->endUndoAction();
    return true;
  }

  bool indentSelectedLines(EditorState *state, bool unindent)
  {
    ScintillaEdit *editor = state ? state->editor.data() : NULL;
    if(!editor || editor->readOnly() || editor->selectionStart() == editor->selectionEnd() ||
       editor->selections() != 1 || editor->selectionIsRectangle())
      return false;

    const QPair<int, int> lines = selectedLineRange(editor);
    int width = int(editor->indent());
    if(width <= 0)
      width = int(editor->tabWidth());
    width = qMax(1, width);

    editor->beginUndoAction();
    for(int line = lines.first; line <= lines.second; line++)
    {
      const int oldIndent = int(editor->lineIndentation(line));
      editor->setLineIndentation(line, unindent ? qMax(0, oldIndent - width) : oldIndent + width);
    }
    editor->endUndoAction();
    return true;
  }

  bool toggleLineComment(EditorState *state)
  {
    ScintillaEdit *editor = state ? state->editor.data() : NULL;
    if(!editor || editor->readOnly() || editor->selections() != 1 ||
       editor->selectionIsRectangle())
      return false;

    const QByteArray marker = state->language == QStringLiteral("python") ? QByteArray("#")
                                                                          : QByteArray("//");
    const QPair<int, int> lines = selectedLineRange(editor);
    bool anyNonBlank = false;
    bool allCommented = true;
    for(int line = lines.first; line <= lines.second; line++)
    {
      const int start = int(editor->positionFromLine(line));
      const QByteArray content = editor->textRange(start, int(editor->lineEndPosition(line)));
      const int indentation = leadingWhitespace(content).size();
      const QByteArray remainder = content.mid(indentation);
      if(remainder.trimmed().isEmpty())
        continue;
      anyNonBlank = true;
      if(!remainder.startsWith(marker))
        allCommented = false;
    }
    allCommented = anyNonBlank && allCommented;

    QList<TextReplacement> replacements;
    for(int line = lines.first; line <= lines.second; line++)
    {
      const int lineStartPosition = int(editor->positionFromLine(line));
      const QByteArray content =
          editor->textRange(lineStartPosition, int(editor->lineEndPosition(line)));
      const int indentation = leadingWhitespace(content).size();
      const QByteArray remainder = content.mid(indentation);
      TextReplacement replacement;
      replacement.start = lineStartPosition + indentation;
      if(allCommented)
      {
        if(remainder.trimmed().isEmpty() || !remainder.startsWith(marker))
          continue;
        replacement.removedLength = marker.size();
        if(remainder.size() > marker.size() && remainder[marker.size()] == ' ')
          replacement.removedLength++;
      }
      else
      {
        replacement.inserted = marker + QByteArray(" ");
      }
      replacements.push_back(replacement);
    }

    if(replacements.isEmpty())
      return true;

    const int originalAnchor = int(editor->anchor());
    const int originalCaret = int(editor->currentPos());
    editor->beginUndoAction();
    for(int i = replacements.size() - 1; i >= 0; i--)
    {
      const TextReplacement &replacement = replacements[i];
      editor->setTargetRange(replacement.start, replacement.start + replacement.removedLength);
      editor->replaceTarget(replacement.inserted.size(), replacement.inserted.constData());
    }
    editor->setSel(transformedPosition(originalAnchor, replacements),
                   transformedPosition(originalCaret, replacements));
    editor->endUndoAction();
    return true;
  }

  bool typePairedCharacter(EditorState *state, char typed)
  {
    ScintillaEdit *editor = state ? state->editor.data() : NULL;
    if(!editor || editor->readOnly() || editor->selections() != 1 ||
       editor->selectionIsRectangle())
      return false;

    char closing = '\0';
    if(typed == '(')
      closing = ')';
    else if(typed == '[')
      closing = ']';
    else if(typed == '{')
      closing = '}';
    else if(typed == '\'' || typed == '"')
      closing = typed;

    const int start = int(editor->selectionStart());
    const int end = int(editor->selectionEnd());
    if(start == end && (typed == ')' || typed == ']' || typed == '}' || typed == '\'' ||
                        typed == '"') &&
       editor->charAt(start) == typed)
    {
      editor->setEmptySelection(start + 1);
      return true;
    }

    if(closing == '\0')
      return false;
    if(start == end && (typed == '\'' || typed == '"') && start > 0 &&
       editor->charAt(start - 1) == '\\')
      return false;

    const QByteArray selected = editor->textRange(start, end);
    QByteArray replacement;
    replacement.reserve(selected.size() + 2);
    replacement.append(typed);
    replacement.append(selected);
    replacement.append(closing);

    const bool forwardSelection = editor->anchor() <= editor->currentPos();
    editor->beginUndoAction();
    editor->setTargetRange(start, end);
    editor->replaceTarget(replacement.size(), replacement.constData());
    if(start == end)
      editor->setEmptySelection(start + 1);
    else if(forwardSelection)
      editor->setSel(start + 1, start + 1 + selected.size());
    else
      editor->setSel(start + 1 + selected.size(), start + 1);
    editor->endUndoAction();

    if(typed == '(')
      callTip(state);
    return true;
  }

  bool deleteEmptyPair(EditorState *state)
  {
    ScintillaEdit *editor = state ? state->editor.data() : NULL;
    if(!editor || editor->readOnly() || editor->selections() != 1 ||
       editor->selectionIsRectangle() || editor->selectionStart() != editor->selectionEnd())
      return false;

    const int caret = int(editor->currentPos());
    if(caret <= 0 || caret >= editor->length())
      return false;

    const char previous = char(editor->charAt(caret - 1));
    const char next = char(editor->charAt(caret));
    const bool paired = (previous == '(' && next == ')') || (previous == '[' && next == ']') ||
                        (previous == '{' && next == '}') ||
                        (previous == '\'' && next == '\'') ||
                        (previous == '"' && next == '"');
    if(!paired)
      return false;

    editor->beginUndoAction();
    editor->setTargetRange(caret - 1, caret + 1);
    editor->replaceTarget(0, "");
    editor->setEmptySelection(caret - 1);
    editor->endUndoAction();
    return true;
  }

  void updateSymbolHighlights(EditorState *state)
  {
    ScintillaEdit *editor = state ? state->editor.data() : NULL;
    if(!editor || state->textDirty)
      return;
    const int previous = int(editor->indicatorCurrent());
    if(state->maskedVersion != state->version)
    {
      state->maskedText = maskedCode(state->text, state->language);
      state->maskedVersion = state->version;
    }
    const QByteArray &masked = state->maskedText;
    if(state->coloredVersion != state->version)
    {
      state->coloredVersion = state->version;
      for(int indicator : {FunctionNameIndicator, VariableNameIndicator})
      {
        editor->setIndicatorCurrent(indicator);
        editor->indicatorClearRange(0, editor->length());
      }
      if(state->language != QStringLiteral("python") && masked.size() <= MaxMessageBytes)
      {
        QSet<QByteArray> functions, variables;
        for(const Symbol &symbol : state->symbols)
          (symbol.signature.isEmpty() ? variables : functions).insert(symbol.name.toUtf8());
        for(int start = 0; start < masked.size();)
        {
          if(!isIdentifierByte(masked[start]))
          {
            start++;
            continue;
          }
          int end = start + 1;
          while(end < masked.size() && isIdentifierByte(masked[end]))
            end++;
          const QByteArray word = masked.mid(start, end - start);
          const int indicator = functions.contains(word) ? FunctionNameIndicator
                                : variables.contains(word) ? VariableNameIndicator : -1;
          if(indicator >= 0)
          {
            editor->setIndicatorCurrent(indicator);
            editor->indicatorFillRange(start, end - start);
          }
          start = end;
        }
      }
    }
    const QByteArray word = identifierAt(masked, int(editor->currentPos())).toUtf8();
    if(state->highlightedWord != word || state->highlightedVersion != state->version)
    {
      state->highlightedWord = word;
      state->highlightedVersion = state->version;
      editor->setIndicatorCurrent(SymbolOccurrenceIndicator);
      editor->indicatorClearRange(0, editor->length());
      if(!word.isEmpty() && word.size() <= 128 && masked.size() <= MaxMessageBytes)
      {
        int start = 0;
        for(int count = 0; count < 10000; count++)
        {
          start = masked.indexOf(word, start);
          if(start < 0)
            break;
          const int end = start + word.size();
          if((start == 0 || !isIdentifierByte(masked[start - 1])) &&
             (end == masked.size() || !isIdentifierByte(masked[end])))
            editor->indicatorFillRange(start, word.size());
          start = end;
        }
      }
    }
    editor->setIndicatorCurrent(previous);
  }

  void updateBraceHighlight(EditorState *state)
  {
    ScintillaEdit *editor = state ? state->editor.data() : NULL;
    if(!editor)
      return;

    editor->braceHighlight(-1, -1);
    editor->braceBadLight(-1);
    const int caret = int(editor->currentPos());
    auto isBrace = [](int character) {
      return character == '(' || character == ')' || character == '[' || character == ']' ||
             character == '{' || character == '}';
    };
    int brace = -1;
    if(caret < editor->length() && isBrace(int(editor->charAt(caret))))
      brace = caret;
    else if(caret > 0 && isBrace(int(editor->charAt(caret - 1))))
      brace = caret - 1;
    if(brace < 0)
      return;

    const int match = int(editor->braceMatch(brace, 0));
    if(match >= 0)
      editor->braceHighlight(brace, match);
    else
      editor->braceBadLight(brace);
  }

  NavigationEntry navigationEntry(ScintillaEdit *editor, int position = -1) const
  {
    NavigationEntry ret;
    EditorState *state = stateFor(editor);
    if(!state || !state->editor)
      return ret;
    ret.editor = state->editor;
    ret.fileName = state->fileName;
    ret.position = position < 0 ? int(editor->currentPos()) : position;
    ret.position = qBound(0, ret.position, int(editor->length()));
    ret.line = int(editor->lineFromPosition(ret.position));
    ret.column = ret.position - int(editor->positionFromLine(ret.line));
    return ret;
  }

  Location navigationLocation(const NavigationEntry &entry) const
  {
    Location ret;
    ret.editor = entry.editor.data();
    ret.fileName = entry.fileName;
    ret.position = entry.position;
    ret.line = entry.line;
    ret.column = entry.column;
    if(!ret.editor && !ret.fileName.isEmpty())
    {
      for(EditorState *state : states)
      {
        if(state->editor && state->fileName.compare(ret.fileName, Qt::CaseInsensitive) == 0)
        {
          ret.editor = state->editor.data();
          ret.position = qBound(0, ret.position, int(ret.editor->length()));
          ret.line = int(ret.editor->lineFromPosition(ret.position));
          ret.column = ret.position - int(ret.editor->positionFromLine(ret.line));
          break;
        }
      }
    }
    return ret;
  }

  void pushHistory(QList<NavigationEntry> &history, const NavigationEntry &entry)
  {
    if(!entry.editor && entry.fileName.isEmpty())
      return;
    if(!history.isEmpty())
    {
      const NavigationEntry &last = history.back();
      if(last.editor == entry.editor && last.fileName == entry.fileName &&
         last.position == entry.position)
        return;
    }
    history.push_back(entry);
    while(history.size() > NavigationHistoryLimit)
      history.pop_front();
  }

  void navigateToDefinition(const Location &location, ScintillaEdit *sourceEditor,
                            int sourcePosition)
  {
    const NavigationEntry source = navigationEntry(sourceEditor, sourcePosition);
    if((location.editor != source.editor || location.position != source.position) &&
       (source.editor || !source.fileName.isEmpty()))
    {
      pushHistory(backHistory, source);
      forwardHistory.clear();
    }
    deliverDefinition(location);
  }

  bool navigateHistory(ScintillaEdit *editor, bool backwards)
  {
    QList<NavigationEntry> &source = backwards ? backHistory : forwardHistory;
    QList<NavigationEntry> &destination = backwards ? forwardHistory : backHistory;
    while(!source.isEmpty())
    {
      const NavigationEntry targetEntry = source.takeLast();
      const Location target = navigationLocation(targetEntry);
      if(!target.editor && (target.fileName.isEmpty() || !definitionHandler))
        continue;
      pushHistory(destination, navigationEntry(editor));
      deliverDefinition(target);
      return true;
    }
    return false;
  }

  void showReferences(EditorState *sourceState, int position)
  {
    if(!sourceState || !sourceState->editor) return;
    // Synchronise every open document before taking the popup snapshot.
    for(EditorState *state : states) syncText(state, false);
    const Location definition = localDefinition(sourceState, position);
    if(!definition.isValid()) return;
    const QString word = identifierAt(sourceState->text, position);
    const QByteArray needle = word.toUtf8();
    if(needle.isEmpty()) return;
    if(referencesMenu) referencesMenu->close();
    const QPointer<ScintillaEdit> origin = sourceState->editor;
    const quint64 originRevision = sourceState->editRevision;

    struct ReferenceHit
    {
      QPointer<ScintillaEdit> editor;
      quint64 revision;
      Location target;
      QString label;
    };
    QVector<ReferenceHit> hits;
    for(EditorState *state : states)
    {
      const QByteArray masked = maskedCode(state->text, state->language);
      int offset = 0;
      while(hits.size() < 256 && (offset = masked.indexOf(needle, offset)) >= 0)
      {
        const int found = offset;
        offset += needle.size();
        if((found > 0 && isIdentifierByte(masked[found - 1])) ||
           (offset < masked.size() && isIdentifierByte(masked[offset]))) continue;
        if(state->editor == definition.editor && found == definition.position) continue;
        const Location resolved = resolveDefinition(state, found, word, 0, &masked);
        if(resolved.editor != definition.editor || resolved.position != definition.position) continue;
        const Location target = navigationLocation(navigationEntry(state->editor, found));
        const int start = int(state->editor->positionFromLine(target.line));
        const int end = int(state->editor->lineEndPosition(target.line));
        QString snippet = QString::fromUtf8(state->text.mid(start, end - start)).simplified().left(140);
        const QString label = QCoreApplication::translate("CodeEditorService", "%1:%2  %3")
                                  .arg(QFileInfo(state->fileName).fileName()).arg(target.line + 1)
                                  .arg(snippet);
        ReferenceHit hit;
        hit.editor = state->editor;
        hit.revision = state->editRevision;
        hit.target = target;
        hit.label = label;
        hits.push_back(hit);
      }
      if(hits.size() >= 256) break;
    }

    // A single call site needs no chooser: jump straight to it (guarding the
    // captured revisions so a concurrent edit cannot mis-navigate).
    if(hits.size() == 1)
    {
      const ReferenceHit &only = hits.front();
      EditorState *from = stateFor(origin.data());
      EditorState *to = stateFor(only.editor.data());
      if(from && to && from->editRevision == originRevision && to->editRevision == only.revision)
        navigateToDefinition(only.target, origin.data(), position);
      return;
    }

    QMenu *menu = new QMenu(sourceState->editor);
    referencesMenu = menu;
    menu->setObjectName(QStringLiteral("shaderReferencesPopup")); // i18n-ignore: automation id.
    menu->setAttribute(Qt::WA_DeleteOnClose);
    menu->addSection(QCoreApplication::translate("CodeEditorService", "References to %1").arg(word));
    for(const ReferenceHit &hit : hits)
    {
      QAction *action = menu->addAction(QString(hit.label).replace(QLatin1Char('&'), QStringLiteral("&&")));
      action->setData(hit.target.line + 1);
      const QPointer<ScintillaEdit> targetEditor = hit.editor;
      const quint64 targetRevision = hit.revision;
      const Location target = hit.target;
      QObject::connect(action, &QAction::triggered, owner,
                       [this, origin, originRevision, targetEditor, targetRevision, target, position]() {
        EditorState *from = stateFor(origin.data());
        EditorState *to = stateFor(targetEditor.data());
        if(!from || !to || from->editRevision != originRevision || to->editRevision != targetRevision)
          return;
        navigateToDefinition(target, origin.data(), position);
      });
    }
    if(hits.isEmpty())
      menu->addAction(QCoreApplication::translate("CodeEditorService", "No references found in open documents"))->setEnabled(false);
    else if(hits.size() >= 256)
      menu->addAction(QCoreApplication::translate("CodeEditorService", "Showing the first 256 references"))->setEnabled(false);
    ScintillaEdit *editor = sourceState->editor;
    const QPoint local(int(editor->pointXFromPosition(position)),
                       int(editor->pointYFromPosition(position) + editor->textHeight(editor->lineFromPosition(position))));
    menu->popup(editor->mapToGlobal(local));
  }

  QStringList completions(EditorState *state, const QString &prefix,
                          const QStringList &additional = QStringList()) const
  {
    QSet<QString> unique;
    for(const QString &keyword : languageKeywords(state->language))
      unique.insert(keyword);
    for(auto it = symbolIndex.constBegin(); it != symbolIndex.constEnd(); ++it)
      unique.insert(it.key());
    for(const QString &item : additional)
    {
      const QString clean = item.trimmed();
      if(!clean.isEmpty() && clean.size() <= 128 && !clean.contains(QLatin1Char('\n')) &&
         !clean.contains(QLatin1Char('\r')))
        unique.insert(clean);
    }

    QStringList ret;
    for(const QString &item : unique)
    {
      if(item == prefix)
        continue;
      if(prefix.isEmpty() || item.startsWith(prefix, Qt::CaseInsensitive))
        ret.push_back(item);
    }
    std::sort(ret.begin(), ret.end(), [](const QString &a, const QString &b) {
      return QString::compare(a, b, Qt::CaseInsensitive) < 0;
    });
    if(ret.size() > MaxCompletionItems)
      ret = ret.mid(0, MaxCompletionItems);
    return ret;
  }

  void showCompletions(EditorState *state, const QString &prefix,
                       const QStringList &additional = QStringList())
  {
    if(!state || !state->editor)
      return;
    const QStringList items = completions(state, prefix, additional);
    if(items.isEmpty())
      return;
    const QByteArray encoded = items.join(QLatin1Char('\n')).toUtf8();
    state->editor->autoCShow(prefix.toUtf8().size(), encoded.constData());
  }

  int callOpenParen(const QByteArray &text, int position) const
  {
    int depth = 0;
    bool inString = false;
    char quote = 0;
    bool escaped = false;
    for(int i = qMin(position - 1, text.size() - 1); i >= 0; i--)
    {
      const char c = text[i];
      if(inString)
      {
        if(c == quote && !escaped)
          inString = false;
        escaped = c == '\\' && !escaped;
        if(c != '\\')
          escaped = false;
        continue;
      }
      if(c == '\'' || c == '"')
      {
        inString = true;
        quote = c;
      }
      else if(c == ')')
      {
        depth++;
      }
      else if(c == '(')
      {
        if(depth == 0)
          return i;
        depth--;
      }
      else if((c == ';' || c == '{' || c == '}') && depth == 0)
      {
        return -1;
      }
    }
    return -1;
  }

  QString builtinSignature(const QString &name) const
  {
    // i18n-ignore: Shader API names and signatures are code tokens shown verbatim in call tips.
    static const QHash<QString, QString> signatures = {
        {QStringLiteral("mul"), QStringLiteral("mul(x, y)")},
        {QStringLiteral("lerp"), QStringLiteral("lerp(x, y, s)")},
        {QStringLiteral("clamp"), QStringLiteral("clamp(x, minValue, maxValue)")},
        {QStringLiteral("smoothstep"), QStringLiteral("smoothstep(min, max, x)")},
        {QStringLiteral("normalize"), QStringLiteral("normalize(x)")},
        {QStringLiteral("dot"), QStringLiteral("dot(x, y)")},
        {QStringLiteral("cross"), QStringLiteral("cross(x, y)")},
        {QStringLiteral("reflect"), QStringLiteral("reflect(incident, normal)")},
        {QStringLiteral("refract"), QStringLiteral("refract(incident, normal, eta)")},
        {QStringLiteral("texture"), QStringLiteral("texture(sampler, coordinate)")},
        {QStringLiteral("texelFetch"), QStringLiteral("texelFetch(sampler, coordinate, lod)")},
    };
    return signatures.value(name);
  }

  void callTip(EditorState *state)
  {
    if(!state || !state->editor)
      return;
    syncText(state, false);
    const int caret = int(state->editor->currentPos());
    const int openParen = callOpenParen(state->text, caret);
    if(openParen < 0)
      return;

    int before = openParen;
    while(before > 0 && (state->text[before - 1] == ' ' || state->text[before - 1] == '\t'))
      before--;
    const QString function = identifierAt(state->text, before);
    if(function.isEmpty())
      return;

    QStringList signatures;
    for(const Symbol &symbol : symbolIndex.value(function))
      if(!symbol.signature.isEmpty() && !signatures.contains(symbol.signature))
        signatures.push_back(symbol.signature);
    if(signatures.isEmpty())
    {
      const QString builtin = builtinSignature(function);
      if(!builtin.isEmpty())
        signatures.push_back(builtin);
    }
    if(signatures.isEmpty())
      return;

    QString tip = signatures[0];
    if(signatures.size() > 1)
    {
      tip += QLatin1Char('\n');
      tip += QCoreApplication::translate("CodeEditorService", "Additional overloads: %1")
                 .arg(signatures.size() - 1);
    }
    const QByteArray encoded = tip.toUtf8();
    state->editor->callTipShow(openParen, encoded.constData());

    int parameter = 0;
    int depth = 0;
    for(int i = openParen + 1; i < caret && i < state->text.size(); i++)
    {
      if(state->text[i] == '(' || state->text[i] == '[' || state->text[i] == '{')
        depth++;
      else if(state->text[i] == ')' || state->text[i] == ']' || state->text[i] == '}')
        depth = qMax(0, depth - 1);
      else if(state->text[i] == ',' && depth == 0)
        parameter++;
    }

    int argumentStart = tip.indexOf(QLatin1Char('(')) + 1;
    for(int i = 0; i < parameter && argumentStart > 0; i++)
      argumentStart = tip.indexOf(QLatin1Char(','), argumentStart) + 1;
    if(argumentStart > 0)
    {
      while(argumentStart < tip.size() && tip[argumentStart].isSpace())
        argumentStart++;
      int argumentEnd = tip.indexOf(QLatin1Char(','), argumentStart);
      const int closing = tip.indexOf(QLatin1Char(')'), argumentStart);
      if(argumentEnd < 0 || (closing >= 0 && closing < argumentEnd))
        argumentEnd = closing;
      if(argumentEnd > argumentStart)
      {
        state->editor->callTipSetHlt(tip.left(argumentStart).toUtf8().size(),
                                     tip.left(argumentEnd).toUtf8().size());
      }
    }
  }

  void showDiagnosticAt(EditorState *state, int x, int y)
  {
    if(!state || !state->editor)
      return;
    const int position = int(state->editor->positionFromPointClose(x, y));
    if(position < 0)
      return;
    for(const Diagnostic &diagnostic : combinedDiagnostics(state))
    {
      if(position >= diagnostic.start && position < diagnostic.start + diagnostic.length)
      {
        const QByteArray message = diagnostic.message.left(1024).toUtf8();
        state->editor->callTipShow(position, message.constData());
        state->diagnosticTipActive = true;
        return;
      }
    }
  }

  QString findServerProgram()
  {
    if(!resolvedProgram.isEmpty())
      return resolvedProgram;

    QString candidate = configuredProgram.trimmed();
    if(candidate.isEmpty())
      candidate = QString::fromLocal8Bit(qgetenv("RENDERDOC_SHADER_LANGUAGE_SERVER")).trimmed();
    if(candidate.size() > 1 && candidate.startsWith(QLatin1Char('"')) &&
       candidate.endsWith(QLatin1Char('"')))
      candidate = candidate.mid(1, candidate.size() - 2);

    if(!candidate.isEmpty())
    {
      if(QFileInfo(candidate).isExecutable())
        resolvedProgram = QFileInfo(candidate).absoluteFilePath();
      else
        resolvedProgram = QStandardPaths::findExecutable(candidate);
      return resolvedProgram;
    }

    // The first-party distribution includes a pinned compiler-backed semantic provider.
    const QString bundled = QDir(QCoreApplication::applicationDirPath()).filePath(
        QStringLiteral("integrations/shader-language-server/slangd.exe"));
    if(QFileInfo(bundled).isExecutable())
      resolvedProgram = bundled;
    if(resolvedProgram.isEmpty())
      resolvedProgram = QStandardPaths::findExecutable(QStringLiteral("shader-language-server"));
    if(resolvedProgram.isEmpty())
      resolvedProgram = QStandardPaths::findExecutable(QStringLiteral("slangd"));
    return resolvedProgram;
  }

  bool startServer(bool reportMissing)
  {
    if(process->state() != QProcess::NotRunning)
      return true;
    const QString program = findServerProgram();
    serverAttempted = true;
    if(program.isEmpty())
    {
      if(reportMissing)
        reportStatus(QCoreApplication::translate(
            "CodeEditorService",
            "No shader language server was found; local IDE services remain active"));
      return false;
    }

    receiveBuffer.clear();
    pending.clear();
    serverReady = false;
    definitionProvider = referencesProvider = highlightProvider = false;
    stopping = false;
    lastServerError.clear();

    QStringList arguments = serverArguments;
    const QString serverName = QFileInfo(program).completeBaseName().toLower();
    if(arguments.isEmpty())
    {
      if(serverName == QStringLiteral("shader-language-server"))
      {
        arguments << QStringLiteral("--stdio") << QStringLiteral("--hlsl")
                  << QStringLiteral("--glsl");
        for(EditorState *state : states)
        {
          const QString localPath = pathFromUri(state->uri);
          if(!localPath.isEmpty())
          {
            const QString workspace = QFileInfo(localPath).absolutePath();
            process->setWorkingDirectory(workspace);
            arguments << QStringLiteral("--cwd") << workspace;
            break;
          }
        }
      }
      else if(serverName == QStringLiteral("slangd"))
      {
        arguments << QStringLiteral("--stdio");
      }
    }

    process->start(program, arguments, QIODevice::ReadWrite);
    return true;
  }

  bool sendJson(const QJsonObject &object)
  {
    if(process->state() != QProcess::Running)
      return false;
    const QByteArray body = QJsonDocument(object).toJson(QJsonDocument::Compact);
    if(body.isEmpty() || body.size() > MaxMessageBytes)
    {
      reportStatus(QCoreApplication::translate(
          "CodeEditorService",
          "Language-server message was rejected because it exceeded the safety limit"));
      return false;
    }
    const QByteArray header = QByteArrayLiteral("Content-Length: ") +
                              QByteArray::number(body.size()) + QByteArrayLiteral("\r\n\r\n");
    return process->write(header) == header.size() && process->write(body) == body.size();
  }

  int sendRequest(const QString &method, const QJsonObject &params, const PendingRequest &request)
  {
    const int id = nextRequestId++;
    QJsonObject object;
    object[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
    object[QStringLiteral("id")] = id;
    object[QStringLiteral("method")] = method;
    object[QStringLiteral("params")] = params;
    if(!sendJson(object))
      return -1;
    pending[id] = request;
    return id;
  }

  bool sendNotification(const QString &method, const QJsonObject &params)
  {
    QJsonObject object;
    object[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
    object[QStringLiteral("method")] = method;
    object[QStringLiteral("params")] = params;
    return sendJson(object);
  }

  void sendInitialize()
  {
    QJsonObject capabilities;
    QJsonObject textDocument;
    textDocument[QStringLiteral("completion")] = QJsonObject();
    textDocument[QStringLiteral("definition")] = QJsonObject();
    textDocument[QStringLiteral("references")] = QJsonObject();
    textDocument[QStringLiteral("documentHighlight")] = QJsonObject();
    textDocument[QStringLiteral("publishDiagnostics")] = QJsonObject();
    QJsonObject semanticTokens;
    semanticTokens[QStringLiteral("requests")] = QJsonObject{{QStringLiteral("full"), true}};
    semanticTokens[QStringLiteral("tokenTypes")] = QJsonArray{
        QStringLiteral("type"), QStringLiteral("class"), QStringLiteral("struct"),
        QStringLiteral("interface"), QStringLiteral("enum"), QStringLiteral("typeParameter"),
        QStringLiteral("enumMember"), QStringLiteral("variable"), QStringLiteral("parameter"),
        QStringLiteral("function"), QStringLiteral("method"), QStringLiteral("property"),
        QStringLiteral("namespace"), QStringLiteral("macro"), QStringLiteral("keyword"),
        QStringLiteral("string")};
    semanticTokens[QStringLiteral("tokenModifiers")] = QJsonArray();
    semanticTokens[QStringLiteral("formats")] = QJsonArray{QStringLiteral("relative")};
    semanticTokens[QStringLiteral("overlappingTokenSupport")] = false;
    semanticTokens[QStringLiteral("multilineTokenSupport")] = false;
    textDocument[QStringLiteral("semanticTokens")] = semanticTokens;
    capabilities[QStringLiteral("textDocument")] = textDocument;
    QJsonObject workspace;
    workspace[QStringLiteral("configuration")] = true;
    capabilities[QStringLiteral("workspace")] = workspace;

    QJsonObject clientInfo;
    clientInfo[QStringLiteral("name")] = QStringLiteral("Bread");
    clientInfo[QStringLiteral("version")] = QStringLiteral("1");

    QJsonObject params;
    params[QStringLiteral("processId")] = double(QCoreApplication::applicationPid());
    params[QStringLiteral("clientInfo")] = clientInfo;
    params[QStringLiteral("capabilities")] = capabilities;
    params[QStringLiteral("trace")] = QStringLiteral("off");

    for(EditorState *state : states)
    {
      const QString localPath = pathFromUri(state->uri);
      if(!localPath.isEmpty())
      {
        params[QStringLiteral("rootUri")] =
            QUrl::fromLocalFile(QFileInfo(localPath).absolutePath()).toString(QUrl::FullyEncoded);
        break;
      }
    }

    PendingRequest request;
    request.kind = PendingKind::Initialize;
    const int id = sendRequest(QStringLiteral("initialize"), params, request);
    QTimer::singleShot(15000, owner, [this, id]() {
      if(id < 0 || !pending.contains(id)) return;
      queuedNavigation.clear();
      reportStatus(QCoreApplication::translate("CodeEditorService",
          "Shader language server initialization timed out."));
      stopping = true;
      process->kill();
    });
  }

  void sendDidOpen(EditorState *state)
  {
    if(!serverReady || !supportsSemanticLanguage(state) || state->openSent || state->relocatedMacroInclude)
      return;
    QJsonObject item;
    item[QStringLiteral("uri")] = state->uri;
    item[QStringLiteral("languageId")] = state->language;
    item[QStringLiteral("version")] = state->version;
    item[QStringLiteral("text")] = QString::fromUtf8(state->semanticText);
    QJsonObject params;
    params[QStringLiteral("textDocument")] = item;
    state->openSent = sendNotification(QStringLiteral("textDocument/didOpen"), params);
    if(state->openSent)
    {
      state->synchronizedSemanticText = state->semanticText;
      semanticTimer->start();
    }
  }

  void sendDidClose(EditorState *state)
  {
    if(!serverReady || !state || !state->openSent)
      return;
    QJsonObject document;
    document[QStringLiteral("uri")] = state->uri;
    QJsonObject params;
    params[QStringLiteral("textDocument")] = document;
    sendNotification(QStringLiteral("textDocument/didClose"), params);
    state->openSent = false;
  }

  void sendDidChange(EditorState *state)
  {
    if(!serverReady || !state)
      return;
    if(state->relocatedMacroInclude)
    {
      sendDidClose(state);
      return;
    }
    if(!state->openSent)
    {
      sendDidOpen(state);
      return;
    }
    if(textDocumentSyncKind == 0 ||
       (textDocumentSyncKind == 2 &&
        QFileInfo(resolvedProgram).completeBaseName().compare(QStringLiteral("slangd"),
                                                             Qt::CaseInsensitive) == 0 &&
        slangNeedsWholeDocumentReopen(state->synchronizedSemanticText)))
    {
      // Reopen when changes are unsupported, or for the bundled provider's Unicode EOF bug.
      sendDidClose(state);
      sendDidOpen(state);
      return;
    }

    QJsonObject document;
    document[QStringLiteral("uri")] = state->uri;
    document[QStringLiteral("version")] = state->version;
    QJsonArray changes;
    changes.append(lspContentChange(textDocumentSyncKind, state->synchronizedSemanticText,
                                   state->semanticText));
    QJsonObject params;
    params[QStringLiteral("textDocument")] = document;
    params[QStringLiteral("contentChanges")] = changes;
    if(sendNotification(QStringLiteral("textDocument/didChange"), params))
      state->synchronizedSemanticText = state->semanticText;
    semanticTimer->start();
  }

  void requestSemanticTokens(EditorState *state)
  {
    if(!serverReady || !semanticTokensProvider || !state || !state->editor ||
       !state->openSent || state->textDirty || state->relocatedMacroInclude ||
       state->semanticRequestedVersion == state->version)
      return;
    state->semanticRequestedVersion = state->version;
    PendingRequest request;
    request.kind = PendingKind::SemanticTokens;
    request.editor = state->editor;
    request.sourceEditor = state->editor.data();
    request.version = state->version;
    request.editRevision = state->editRevision;
    for(EditorState *document : states)
      if(documentInScope(state, document->fileName, document))
        request.documentRevisions.insert(document->editor.data(), document->editRevision);
    QJsonObject document;
    document[QStringLiteral("uri")] = state->uri;
    QJsonObject params;
    params[QStringLiteral("textDocument")] = document;
    const int id = sendRequest(QStringLiteral("textDocument/semanticTokens/full"), params, request);
    if(id >= 0)
      QTimer::singleShot(15000, owner, [this, id]() {
        if(!pending.contains(id)) return;
        QJsonObject params;
        params[QStringLiteral("id")] = id;
        sendNotification(QStringLiteral("$/cancelRequest"), params);
        pending.remove(id);
        // A slow or unsupported provider must not disable editing or produce repeated popups.
        // Retain lexical colors, and retry only when the document changes.
      });
  }

  void paintSemanticTokens(const QPointer<ScintillaEdit> &editor, int version,
                           quint64 editRevision, quint64 generation,
                           const std::shared_ptr<QVector<SemanticColorRange>> &ranges, int offset)
  {
    EditorState *state = stateFor(editor.data());
    if(!state || !editor || state->version != version || state->editRevision != editRevision ||
       state->textDirty || state->semanticPaintGeneration != generation)
      return;
    const int previousIndicator = int(editor->indicatorCurrent());
    const int previousValue = int(editor->indicatorValue());
    editor->setIndicatorCurrent(SemanticNameIndicator);
    if(offset == 0) editor->indicatorClearRange(0, editor->length());
    const int end = qMin(offset + SemanticPaintBatchSize, ranges->size());
    for(int i = offset; i < end; ++i)
    {
      const SemanticColorRange &range = (*ranges)[i];
      editor->setIndicatorValue(SC_INDICVALUEBIT | range.color);
      editor->indicatorFillRange(range.start, range.length);
    }
    editor->setIndicatorCurrent(previousIndicator);
    editor->setIndicatorValue(previousValue);
    if(end < ranges->size())
      QTimer::singleShot(0, owner, [this, editor, version, editRevision, generation, ranges, end]() {
        paintSemanticTokens(editor, version, editRevision, generation, ranges, end);
      });
  }

  void requestLspCompletion(EditorState *state, const QString &prefix)
  {
    if(!serverReady || !state || !state->openSent || !state->editor)
      return;
    QJsonObject document;
    document[QStringLiteral("uri")] = state->uri;
    QJsonObject params;
    params[QStringLiteral("textDocument")] = document;
    params[QStringLiteral("position")] = lspPosition(state->text, int(state->editor->currentPos()));
    QJsonObject context;
    context[QStringLiteral("triggerKind")] = 1;
    params[QStringLiteral("context")] = context;
    PendingRequest request;
    request.kind = PendingKind::Completion;
    request.editor = state->editor;
    request.sourceEditor = state->editor.data();
    request.prefix = prefix;
    request.version = state->version;
    request.editRevision = state->editRevision;
    request.position = int(state->editor->currentPos());
    sendRequest(QStringLiteral("textDocument/completion"), params, request);
  }

  void cancelRequests(ScintillaEdit *editor, bool dependencies)
  {
    if(dependencies) referenceCache.clear();
    queuedNavigation.remove(editor);
    for(auto it = referenceSearches.begin(); it != referenceSearches.end();)
    {
      if(it.key() == editor || (dependencies && it->origin.documentRevisions.contains(editor)))
        it = referenceSearches.erase(it);
      else ++it;
    }
    for(auto it = pending.begin(); it != pending.end();)
    {
      // Moving the caret or requesting F12 cancels navigation, not document coloring.
      if(it->kind == PendingKind::SemanticTokens && !dependencies)
      {
        ++it;
        continue;
      }
      if(it->sourceEditor != editor &&
         !(dependencies && it->documentRevisions.contains(editor)))
      {
        ++it;
        continue;
      }
      QJsonObject params;
      params[QStringLiteral("id")] = it.key();
      sendNotification(QStringLiteral("$/cancelRequest"), params);
      it = pending.erase(it);
    }
  }

  void cancelMovedNavigation(EditorState *state)
  {
    if(!state || !state->editor) return;
    const int position = int(state->editor->currentPos());
    const auto queued = queuedNavigation.constFind(state->editor.data());
    bool moved = queued != queuedNavigation.constEnd() && queued->position != position;
    for(const PendingRequest &request : pending)
      if(request.kind != PendingKind::SemanticTokens &&
         request.sourceEditor == state->editor.data() && request.position != position)
        moved = true;
    if(moved) cancelRequests(state->editor.data(), false);
  }

  bool requestLspNavigation(EditorState *state, int position, bool references)
  {
    if(!supportsSemanticLanguage(state) || !state->editor || findServerProgram().isEmpty()) return false;
    // A semantic request owns this action even while the server starts. Never turn a
    // startup race, an unresolved overload, or an empty compiler result into a name match.
    cancelRequests(state->editor.data(), false);
    for(EditorState *document : states) syncText(document, false);
    if(state->relocatedMacroInclude)
    {
      reportStatus(QCoreApplication::translate("CodeEditorService",
          "Semantic navigation for macro includes is unavailable in relocated previews. Open the source document to navigate this symbol."));
      return true;
    }
    PendingRequest request;
    request.kind = references ? PendingKind::References : PendingKind::Definition;
    request.editor = state->editor;
    request.sourceEditor = state->editor.data();
    request.version = state->version;
    request.editRevision = state->editRevision;
    request.position = position;
    for(EditorState *document : states)
      if(documentInScope(state, document->fileName, document))
        request.documentRevisions.insert(document->editor.data(), document->editRevision);
    if(!serverReady)
    {
      if(startServer(false)) queuedNavigation[state->editor.data()] = request;
      return true;
    }
    QString method = QStringLiteral("textDocument/definition");
    if(references)
    {
      if(referencesProvider) method = QStringLiteral("textDocument/references");
      else if(highlightProvider)
      {
        method = QStringLiteral("textDocument/documentHighlight");
        request.kind = PendingKind::Highlights;
      }
      else if(definitionProvider)
      {
        // Compiler-backed providers such as slangd expose definitions but no references.
        // Resolve the selected declaration first, then check a bounded set of candidates.
        method = QStringLiteral("textDocument/definition");
        request.kind = PendingKind::ReferenceDefinition;
      }
      else
      {
        reportStatus(QCoreApplication::translate("CodeEditorService",
            "The connected language server does not provide semantic references."));
        return true;
      }
    }
    else if(!definitionProvider)
    {
      reportStatus(QCoreApplication::translate("CodeEditorService",
          "The connected language server does not provide semantic definitions."));
      return true;
    }
    sendDidOpen(state);
    QJsonObject document;
    document[QStringLiteral("uri")] = state->uri;
    QJsonObject params;
    params[QStringLiteral("textDocument")] = document;
    params[QStringLiteral("position")] = lspPosition(state->text, position);
    if(references)
    {
      QJsonObject context;
      context[QStringLiteral("includeDeclaration")] = false;
      params[QStringLiteral("context")] = context;
    }
    const int id = sendRequest(method, params, request);
    if(id >= 0)
      QTimer::singleShot(8000, owner, [this, id]() {
        if(!pending.contains(id)) return;
        QJsonObject params;
        params[QStringLiteral("id")] = id;
        sendNotification(QStringLiteral("$/cancelRequest"), params);
        pending.remove(id);
        reportStatus(QCoreApplication::translate("CodeEditorService",
            "Semantic navigation timed out. The editor remains available; try again after indexing."));
      });
    return true;
  }

  bool sameSemanticLocation(const Location &a, const Location &b) const
  {
    return a.line == b.line && a.column == b.column &&
           QDir::cleanPath(QFileInfo(a.fileName).absoluteFilePath()).compare(
               QDir::cleanPath(QFileInfo(b.fileName).absoluteFilePath()), Qt::CaseInsensitive) == 0;
  }

  QString semanticLocationKey(const QJsonValue &result) const
  {
    const QJsonObject value = result.toObject();
    const QString uri = value.value(value.contains(QStringLiteral("targetUri"))
                                       ? QStringLiteral("targetUri") : QStringLiteral("uri")).toString();
    const QString file = pathFromUri(uri);
    if(file.isEmpty()) return QString();
    const QJsonObject range = value.value(value.contains(QStringLiteral("targetSelectionRange"))
                                             ? QStringLiteral("targetSelectionRange")
                                             : QStringLiteral("range")).toObject();
    if(!range.contains(QStringLiteral("start")) || !range.contains(QStringLiteral("end")))
      return QString();
    return QDir::cleanPath(QFileInfo(file).absoluteFilePath()).toCaseFolded() +
           QString::fromUtf8(QJsonDocument(range).toJson(QJsonDocument::Compact));
  }

  void pumpReferenceSearch(ScintillaEdit *editor)
  {
    auto it = referenceSearches.find(editor);
    if(it == referenceSearches.end()) return;
    ReferenceSearch &search = it.value();
    while(search.outstanding < 8 && !search.candidates.isEmpty())
    {
      const Location candidate = search.candidates.takeFirst();
      EditorState *document = stateFor(candidate.editor);
      if(!document) continue;
      PendingRequest request = search.origin;
      request.kind = PendingKind::ReferenceCandidate;
      request.referenceGeneration = search.generation;
      request.referenceCandidate = candidate;
      request.candidateEditor = document->editor;
      QJsonObject params;
      QJsonObject textDocument;
      textDocument[QStringLiteral("uri")] = document->uri;
      params[QStringLiteral("textDocument")] = textDocument;
      params[QStringLiteral("position")] = lspPosition(document->text, candidate.position);
      if(sendRequest(QStringLiteral("textDocument/definition"), params, request) >= 0)
        ++search.outstanding;
    }
    if(search.outstanding || !search.candidates.isEmpty()) return;
    const ReferenceSearch complete = search;
    referenceSearches.erase(it);
    EditorState *source = stateFor(editor);
    if(!source) return;
    if(referenceCache.size() >= 32) referenceCache.clear();
    if(!complete.truncated) referenceCache[complete.cacheKey] = complete.hits;
    showSemanticLocations(source, complete.origin, complete.hits);
    if(complete.truncated)
      reportStatus(QCoreApplication::translate("CodeEditorService",
          "Semantic reference search checked the first 256 candidates in this compilation unit."));
  }

  void startReferenceSearch(EditorState *source, const PendingRequest &request, const Location &definition,
                            const QString &definitionKey)
  {
    ReferenceSearch search;
    search.origin = request;
    search.origin.kind = PendingKind::References;
    search.definition = definition;
    search.definitionKey = definitionKey;
    QStringList revisions;
    for(auto it = request.documentRevisions.constBegin(); it != request.documentRevisions.constEnd(); ++it)
      if(stateFor(it.key()))
        revisions.push_back(stateFor(it.key())->uri + QString::number(it.value()));
    revisions.sort();
    search.cacheKey = source->uri + definitionKey + revisions.join(QLatin1Char('\n'));
    if(referenceCache.contains(search.cacheKey))
    {
      showSemanticLocations(source, search.origin, referenceCache.value(search.cacheKey));
      return;
    }
    search.generation = nextReferenceGeneration++;
    const QByteArray needle = identifierAt(source->text, request.position).toUtf8();
    if(needle.isEmpty()) return;
    reportStatus(QCoreApplication::translate("CodeEditorService",
        "Finding semantic references in open documents of this compilation unit..."));
    // Token scanning only limits work. A hit is accepted exclusively after the compiler
    // resolves it to the exact declaration, so overloads, fields and shadows stay distinct.
    for(EditorState *document : states)
    {
      if(!request.documentRevisions.contains(document->editor.data())) continue;
      const QByteArray masked = maskedCode(document->text, document->language);
      int offset = 0;
      while((offset = masked.indexOf(needle, offset)) >= 0)
      {
        const int found = offset;
        offset += needle.size();
        if((found && isIdentifierByte(masked[found - 1])) ||
           (offset < masked.size() && isIdentifierByte(masked[offset]))) continue;
        Location candidate = navigationLocation(navigationEntry(document->editor.data(), found));
        if(sameSemanticLocation(candidate, definition)) continue;
        if(search.candidates.size() == 256) { search.truncated = true; break; }
        search.candidates.push_back(candidate);
      }
      if(search.truncated) break;
    }
    referenceSearches[source->editor.data()] = search;
    const QPointer<ScintillaEdit> editor = source->editor;
    const int generation = search.generation;
    QTimer::singleShot(8000, owner, [this, editor, generation]() {
      if(!editor || !referenceSearches.contains(editor.data()) ||
         referenceSearches[editor.data()].generation != generation) return;
      cancelRequests(editor.data(), false);
      reportStatus(QCoreApplication::translate("CodeEditorService",
          "Semantic navigation timed out. The editor remains available; try again after indexing."));
    });
    pumpReferenceSearch(source->editor.data());
  }

  void rejectServerRequest(const QJsonObject &object)
  {
    QJsonObject error;
    error[QStringLiteral("code")] = -32601;
    // i18n-ignore: JSON-RPC error text is sent to the language server, not displayed in qrenderdoc.
    error[QStringLiteral("message")] = QStringLiteral("Client-side server commands are disabled");
    QJsonObject response;
    response[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
    response[QStringLiteral("id")] = object.value(QStringLiteral("id"));
    response[QStringLiteral("error")] = error;
    sendJson(response);
  }

  void readServerOutput()
  {
    receiveBuffer += process->readAllStandardOutput();
    if(receiveBuffer.size() > MaxMessageBytes + MaxHeaderBytes)
    {
      reportStatus(QCoreApplication::translate(
          "CodeEditorService", "Language server was stopped after exceeding the receive limit"));
      stopping = true;
      process->kill();
      receiveBuffer.clear();
      return;
    }

    while(true)
    {
      const int headerEnd = receiveBuffer.indexOf("\r\n\r\n");
      if(headerEnd < 0)
      {
        if(receiveBuffer.size() > MaxHeaderBytes)
        {
          reportStatus(QCoreApplication::translate("CodeEditorService",
                                                   "Language server sent an invalid header"));
          stopping = true;
          process->kill();
          receiveBuffer.clear();
        }
        return;
      }

      int contentLength = -1;
      const QList<QByteArray> headers = receiveBuffer.left(headerEnd).split('\n');
      for(QByteArray header : headers)
      {
        header = header.trimmed();
        const int colon = header.indexOf(':');
        if(colon > 0 &&
           header.left(colon).trimmed().toLower() == QByteArrayLiteral("content-length"))
        {
          bool ok = false;
          contentLength = header.mid(colon + 1).trimmed().toInt(&ok);
          if(!ok)
            contentLength = -1;
        }
      }
      if(contentLength < 0 || contentLength > MaxMessageBytes)
      {
        reportStatus(QCoreApplication::translate(
            "CodeEditorService", "Language server sent an invalid message length"));
        stopping = true;
        process->kill();
        receiveBuffer.clear();
        return;
      }

      const int bodyStart = headerEnd + 4;
      if(receiveBuffer.size() - bodyStart < contentLength)
        return;
      const QByteArray body = receiveBuffer.mid(bodyStart, contentLength);
      receiveBuffer.remove(0, bodyStart + contentLength);

      QJsonParseError error;
      const QJsonDocument document = QJsonDocument::fromJson(body, &error);
      if(error.error == QJsonParseError::NoError && document.isObject())
        handleServerMessage(document.object());
    }
  }

  void handlePublishDiagnostics(const QJsonObject &params)
  {
    EditorState *state = stateForUri(params.value(QStringLiteral("uri")).toString());
    if(!state)
      return;

    // A diagnostic notification can race the debounce timer after a local edit. Synchronise the
    // document first so versioned diagnostics are compared against the text currently displayed,
    // and discard an unversioned notification that was already in flight for the previous text.
    const bool hadUnsynchronisedEdit = state->textDirty;
    if(hadUnsynchronisedEdit)
      syncText(state, false);

    const QJsonValue versionValue = params.value(QStringLiteral("version"));
    if(versionValue.isDouble())
    {
      const int diagnosticVersion = versionValue.toInt(-1);
      if(diagnosticVersion != state->version)
        return;
    }
    else if(hadUnsynchronisedEdit)
    {
      return;
    }

    state->lspDiagnostics.clear();
    const QJsonArray values = params.value(QStringLiteral("diagnostics")).toArray();
    const int count = qMin(values.size(), MaxDiagnostics);
    for(int i = 0; i < count; i++)
    {
      const QJsonObject value = values[i].toObject();
      const QJsonObject range = value.value(QStringLiteral("range")).toObject();
      const int start = bytePosition(state->text, range.value(QStringLiteral("start")).toObject());
      const int end = bytePosition(state->text, range.value(QStringLiteral("end")).toObject());
      Diagnostic diagnostic;
      diagnostic.start = start;
      diagnostic.length = qMax(1, end - start);
      diagnostic.line = int(state->editor ? state->editor->lineFromPosition(start) : 0);
      diagnostic.column =
          state->editor ? start - int(state->editor->positionFromLine(diagnostic.line)) : 0;
      const int severity = value.value(QStringLiteral("severity")).toInt(1);
      diagnostic.severity = severity == 1   ? DiagnosticSeverity::Error
                            : severity == 2 ? DiagnosticSeverity::Warning
                            : severity == 3 ? DiagnosticSeverity::Information
                                            : DiagnosticSeverity::Hint;
      diagnostic.message = value.value(QStringLiteral("message")).toString().left(2048);
      diagnostic.source = value.value(QStringLiteral("source")).toString().left(128);
      state->lspDiagnostics.push_back(diagnostic);
    }
    displayDiagnostics(state);
  }

  QStringList completionLabels(const QJsonValue &result) const
  {
    QJsonArray items;
    if(result.isArray())
      items = result.toArray();
    else if(result.isObject())
      items = result.toObject().value(QStringLiteral("items")).toArray();

    QStringList labels;
    const int count = qMin(items.size(), MaxCompletionItems);
    for(int i = 0; i < count; i++)
    {
      if(items[i].isString())
        labels.push_back(items[i].toString());
      else
        labels.push_back(items[i].toObject().value(QStringLiteral("label")).toString());
    }
    return labels;
  }

  Location locationFromLsp(const QJsonValue &result) const
  {
    QJsonObject value;
    if(result.isArray())
    {
      const QJsonArray array = result.toArray();
      if(array.isEmpty())
        return Location();
      value = array[0].toObject();
    }
    else
    {
      value = result.toObject();
    }

    const QString uri = value.contains(QStringLiteral("targetUri"))
                            ? value.value(QStringLiteral("targetUri")).toString()
                            : value.value(QStringLiteral("uri")).toString();
    const QJsonObject range = value.contains(QStringLiteral("targetSelectionRange"))
                                  ? value.value(QStringLiteral("targetSelectionRange")).toObject()
                                  : value.value(QStringLiteral("range")).toObject();
    const QJsonObject start = range.value(QStringLiteral("start")).toObject();
    Location location;
    EditorState *state = stateForUri(uri);
    location.fileName = state ? state->fileName : pathFromUri(uri);
    location.line = start.value(QStringLiteral("line")).toInt(-1);
    location.column = start.value(QStringLiteral("character")).toInt(-1);
    if(state && state->editor)
    {
      location.editor = state->editor.data();
      location.position = bytePosition(state->text, start);
      location.line = int(location.editor->lineFromPosition(location.position));
      location.column = location.position - int(location.editor->positionFromLine(location.line));
    }
    return location;
  }

  void showSemanticLocations(EditorState *source, const PendingRequest &request,
                             const QList<Location> &locations)
  {
    if(locations.isEmpty())
    {
      reportStatus(QCoreApplication::translate("CodeEditorService",
          "The language server found no semantic target for the selected symbol."));
      return;
    }
    if(locations.size() == 1)
    {
      navigateToDefinition(locations.front(), source->editor.data(), request.position);
      return;
    }
    if(referencesMenu) referencesMenu->close();
    QMenu *menu = new QMenu(source->editor);
    referencesMenu = menu;
    menu->setObjectName(QStringLiteral("shaderReferencesPopup")); // i18n-ignore: automation id.
    menu->setAttribute(Qt::WA_DeleteOnClose);
    const QString word = identifierAt(source->text, request.position);
    menu->addSection(request.kind == PendingKind::Definition
        ? QCoreApplication::translate("CodeEditorService", "Definitions of %1").arg(word)
        : QCoreApplication::translate("CodeEditorService", "References to %1").arg(word));
    for(const Location &location : locations)
    {
      QAction *action = menu->addAction(
          QCoreApplication::translate("CodeEditorService", "%1:%2")
              .arg(QFileInfo(location.fileName).fileName()).arg(location.line + 1));
      action->setData(location.line + 1);
      const QPointer<ScintillaEdit> target = location.editor;
      const bool openTarget = location.editor != NULL;
      QObject::connect(action, &QAction::triggered, owner,
                       [this, request, location, target, openTarget]() {
        EditorState *origin = stateFor(request.editor.data());
        if(!origin || origin->editRevision != request.editRevision || (openTarget && !target)) return;
        for(auto it = request.documentRevisions.constBegin(); it != request.documentRevisions.constEnd(); ++it)
          if(!stateFor(it.key()) || stateFor(it.key())->editRevision != it.value()) return;
        navigateToDefinition(location, request.editor.data(), request.position);
      });
    }
    ScintillaEdit *editor = source->editor;
    const QPoint local(int(editor->pointXFromPosition(request.position)),
                       int(editor->pointYFromPosition(request.position) +
                           editor->textHeight(editor->lineFromPosition(request.position))));
    menu->popup(editor->mapToGlobal(local));
  }

  void handleServerResponse(const QJsonObject &object)
  {
    const int id = object.value(QStringLiteral("id")).toInt(-1);
    if(!pending.contains(id))
      return;
    const PendingRequest request = pending.take(id);
    const QJsonValue result = object.value(QStringLiteral("result"));

    if(request.kind == PendingKind::Initialize)
    {
      if(object.contains(QStringLiteral("error")))
      {
        reportStatus(QCoreApplication::translate(
            "CodeEditorService", "Shader language server rejected initialization"));
        stopping = true;
        process->kill();
        return;
      }
      serverReady = true;
      const QJsonObject capabilities = result.toObject().value(QStringLiteral("capabilities")).toObject();
      const QJsonValue sync = capabilities.value(QStringLiteral("textDocumentSync"));
      textDocumentSyncKind = sync.isObject()
                                 ? sync.toObject().value(QStringLiteral("change")).toInt(1)
                                 : sync.toInt(1);
      auto supported = [&](const char *key) {
        const QJsonValue value = capabilities.value(QString::fromLatin1(key));
        return value.isObject() || value.toBool(false);
      };
      definitionProvider = supported("definitionProvider");
      referencesProvider = supported("referencesProvider");
      highlightProvider = supported("documentHighlightProvider");
      const QJsonObject semanticProvider =
          capabilities.value(QStringLiteral("semanticTokensProvider")).toObject();
      const QJsonValue fullTokens = semanticProvider.value(QStringLiteral("full"));
      semanticTokensProvider = fullTokens.toBool(false) || fullTokens.isObject();
      semanticTokenTypes.clear();
      for(const QJsonValue &type : semanticProvider.value(QStringLiteral("legend")).toObject()
                                      .value(QStringLiteral("tokenTypes")).toArray())
        semanticTokenTypes.push_back(type.toString());
      semanticTokensProvider &= !semanticTokenTypes.isEmpty();
      sendNotification(QStringLiteral("initialized"), QJsonObject());
      for(EditorState *state : states)
        sendDidOpen(state);
      const auto queued = queuedNavigation;
      queuedNavigation.clear();
      for(const PendingRequest &navigation : queued)
      {
        EditorState *state = stateFor(navigation.editor.data());
        if(state && state->editRevision == navigation.editRevision &&
           state->editor->currentPos() == navigation.position)
          requestLspNavigation(state, navigation.position, navigation.kind == PendingKind::References);
      }
      reportStatus(QCoreApplication::translate("CodeEditorService",
                                               "Shader language server connected: %1")
                       .arg(resolvedProgram));
    }
    else if(request.kind == PendingKind::Completion)
    {
      EditorState *state = stateFor(request.editor.data());
      if(state && state->version == request.version &&
         state->editRevision == request.editRevision && state->editor &&
         state->editor->currentPos() == request.position)
        showCompletions(state, request.prefix, completionLabels(result));
    }
    else if(request.kind == PendingKind::SemanticTokens)
    {
      EditorState *state = stateFor(request.editor.data());
      if(!state || !state->editor || state->version != request.version ||
         state->editRevision != request.editRevision || state->textDirty ||
         object.contains(QStringLiteral("error")))
        return;
      for(auto it = request.documentRevisions.constBegin(); it != request.documentRevisions.constEnd(); ++it)
        if(!stateFor(it.key()) || stateFor(it.key())->editRevision != it.value()) return;
      const QJsonValue data = result.toObject().value(QStringLiteral("data"));
      if(!data.isArray()) return;
      auto ranges = std::make_shared<QVector<SemanticColorRange>>();
      const bool dark = state->editor->palette().color(QPalette::Base).lightness() < 128;
      if(!decodeSemanticColorRanges(state->text, data.toArray(), semanticTokenTypes, dark, *ranges))
        return;
      paintSemanticTokens(state->editor, state->version, state->editRevision,
                          state->semanticPaintGeneration = ++semanticPaintSerial, ranges, 0);
    }
    else if(request.kind == PendingKind::Definition || request.kind == PendingKind::References ||
            request.kind == PendingKind::Highlights || request.kind == PendingKind::ReferenceDefinition ||
            request.kind == PendingKind::ReferenceCandidate)
    {
      EditorState *sourceState = stateFor(request.editor.data());
      if(!sourceState || sourceState->version != request.version ||
         sourceState->editRevision != request.editRevision ||
         sourceState->editor->currentPos() != request.position)
        return;
      // The server response uses positions from its request snapshot. Do not apply it if
      // any open target changed in the meantime, even when the source stayed unchanged.
      for(auto it = request.documentRevisions.constBegin(); it != request.documentRevisions.constEnd(); ++it)
      {
        EditorState *documentState = stateFor(it.key());
        if(!documentState || documentState->editRevision != it.value()) return;
      }
      if(object.contains(QStringLiteral("error")))
      {
        cancelRequests(request.sourceEditor, false);
        reportStatus(QCoreApplication::translate("CodeEditorService", "Semantic navigation failed: %1")
                         .arg(object.value(QStringLiteral("error")).toObject()
                                  .value(QStringLiteral("message")).toString().left(1024)));
        return;
      }
      if(request.kind == PendingKind::ReferenceCandidate)
      {
        auto search = referenceSearches.find(request.sourceEditor);
        if(search == referenceSearches.end() || search->generation != request.referenceGeneration ||
           !request.candidateEditor) return;
        const QJsonArray resolved = result.isArray() ? result.toArray() : QJsonArray{result};
        for(const QJsonValue &candidateDefinition : resolved)
          if(!search->definitionKey.isEmpty() &&
             semanticLocationKey(candidateDefinition) == search->definitionKey)
          {
            search->hits.push_back(request.referenceCandidate);
            break;
          }
        --search->outstanding;
        pumpReferenceSearch(request.sourceEditor);
        return;
      }
      QList<Location> locations;
      const QJsonArray values = result.isArray() ? result.toArray() : QJsonArray{result};
      for(const QJsonValue &item : values)
      {
        QJsonObject value = item.toObject();
        if(request.kind == PendingKind::Highlights)
          value[QStringLiteral("uri")] = sourceState->uri;
        Location location = locationFromLsp(value);
        if(location.line < 0 || location.column < 0 ||
           (!location.editor && location.fileName.isEmpty())) continue;
        if(!documentInScope(sourceState, location.fileName, stateFor(location.editor)))
        {
          // A compiler can resolve macro or transitive includes that the lightweight index
          // cannot see. Route that exact URI to the owning document model for dependency
          // authorization; never select an unrelated existing tab by its symbol or basename.
          location.editor = NULL;
          location.position = -1;
        }
        if((request.kind == PendingKind::References || request.kind == PendingKind::Highlights) &&
           location.editor == sourceState->editor &&
           location.position == identifierRange(sourceState->text, request.position).first) continue;
        locations.push_back(location);
        if(locations.size() >= 256) break;
      }
      if(locations.isEmpty() && (request.kind == PendingKind::Definition ||
                                 request.kind == PendingKind::ReferenceDefinition))
      {
        // slangd intentionally returns no definition when the cursor is already on a
        // declaration. Its exact token range can seed reference lookup: every returned
        // hit must still resolve to that URI AND full range through the compiler.
        const auto token = identifierRange(sourceState->text, request.position);
        const Location declaration = localDefinition(sourceState, request.position);
        if(declaration.editor == sourceState->editor && declaration.position == token.first)
        {
          QJsonObject range, identity;
          range[QStringLiteral("start")] = lspPosition(sourceState->text, token.first);
          range[QStringLiteral("end")] = lspPosition(sourceState->text, token.second);
          identity[QStringLiteral("uri")] = sourceState->uri;
          identity[QStringLiteral("range")] = range;
          startReferenceSearch(sourceState, request, declaration, semanticLocationKey(identity));
          return;
        }
      }
      if(request.kind == PendingKind::ReferenceDefinition && locations.size() == 1)
      {
        startReferenceSearch(sourceState, request, locations.front(),
                             semanticLocationKey(values.isEmpty() ? QJsonValue() : values.first()));
        return;
      }
      if(request.kind == PendingKind::Definition && locations.size() == 1 &&
         locations.front().editor == sourceState->editor &&
         locations.front().position == identifierRange(sourceState->text, request.position).first)
        requestLspNavigation(sourceState, request.position, true);
      else showSemanticLocations(sourceState, request, locations);
    }
  }

  void handleServerMessage(const QJsonObject &object)
  {
    if(object.contains(QStringLiteral("method")))
    {
      const QString method = object.value(QStringLiteral("method")).toString();
      if(method == QStringLiteral("textDocument/publishDiagnostics"))
      {
        handlePublishDiagnostics(object.value(QStringLiteral("params")).toObject());
      }
      else if(object.contains(QStringLiteral("id")))
      {
        if(method == QStringLiteral("client/registerCapability"))
        {
          const QJsonArray registrations = object.value(QStringLiteral("params")).toObject()
                                               .value(QStringLiteral("registrations")).toArray();
          bool readOnly = !registrations.isEmpty();
          for(const QJsonValue &registration : registrations)
            readOnly &= registration.toObject().value(QStringLiteral("method")).toString() ==
                        QStringLiteral("workspace/didChangeConfiguration");
          if(readOnly)
          {
            QJsonObject response;
            response[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
            response[QStringLiteral("id")] = object.value(QStringLiteral("id"));
            response[QStringLiteral("result")] = QJsonValue();
            sendJson(response);
            return;
          }
        }
        if(method == QStringLiteral("workspace/configuration"))
        {
          QJsonArray values;
          const QJsonArray items = object.value(QStringLiteral("params")).toObject()
                                      .value(QStringLiteral("items")).toArray();
          for(const QJsonValue &item : items) { Q_UNUSED(item); values.append(QJsonValue()); }
          QJsonObject response;
          response[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
          response[QStringLiteral("id")] = object.value(QStringLiteral("id"));
          response[QStringLiteral("result")] = values;
          sendJson(response);
          return;
        }
        // Do not execute workspace/applyEdit, workspace/executeCommand, or any other server request.
        rejectServerRequest(object);
      }
      return;
    }
    if(object.contains(QStringLiteral("id")))
      handleServerResponse(object);
  }
};

CodeEditorService::CodeEditorService(QObject *parent) : QObject(parent), m_Impl(new Impl(this))
{
}

CodeEditorService::~CodeEditorService()
{
  for(ScintillaEdit *editor : m_Impl->states.keys())
  {
    editor->removeEventFilter(this);
    QObject::disconnect(editor, NULL, this, NULL);
  }
  delete m_Impl;
  m_Impl = NULL;
}

void CodeEditorService::registerEditor(ScintillaEdit *editor, const QString &fileName,
                                       const QString &language, const QString &includeBaseDirectory)
{
  if(!editor)
    return;

  const QString includeBase = includeBaseDirectory.isEmpty()
                                  ? QString() : QDir(includeBaseDirectory).absolutePath();
  Impl::EditorState *state = m_Impl->stateFor(editor);
  if(state)
  {
    if(state->fileName == fileName && state->language == normaliseLanguage(language, fileName) &&
       state->includeBaseDirectory == includeBase)
      return;
    m_Impl->cancelRequests(editor, true);
    m_Impl->sendDidClose(state);
    state->fileName = fileName;
    state->includeBaseDirectory = includeBase;
    state->uri = m_Impl->makeUri(fileName);
    state->language = normaliseLanguage(language, fileName);
    state->version++;
    state->editRevision++;
    state->openSent = false;
    state->semanticRequestedVersion = -1;
    state->semanticPaintGeneration = ++m_Impl->semanticPaintSerial;
    const int previousIndicator = int(editor->indicatorCurrent());
    editor->setIndicatorCurrent(SemanticNameIndicator);
    editor->indicatorClearRange(0, editor->length());
    editor->setIndicatorCurrent(previousIndicator);
    m_Impl->reindexDocument(state);
    m_Impl->sendDidOpen(state);
    m_Impl->updateSymbolHighlights(state);
    return;
  }

  state = new Impl::EditorState;
  state->editor = editor;
  state->fileName = fileName;
  state->includeBaseDirectory = includeBase;
  state->uri = m_Impl->makeUri(fileName);
  state->language = normaliseLanguage(language, fileName);
  state->text = editor->getText(editor->textLength() + 1);
  state->sourceSha256 = QString::fromLatin1(
      QCryptographicHash::hash(state->text, QCryptographicHash::Sha256).toHex());
  state->localDiagnostics = m_Impl->localDiagnosticsFor(state);
  m_Impl->states[editor] = state;
  editor->installEventFilter(this);
  m_Impl->configureIndicators(editor);
  m_Impl->reindexDocument(state);
  m_Impl->displayDiagnostics(state);

  QObject::connect(editor, &ScintillaEdit::destroyed, this,
                   [this, editor](QObject *) { unregisterEditor(editor); });
  QObject::connect(editor, &ScintillaEdit::modified, this,
                   [this, state](int type, int position, int length, int, const QByteArray &, int, int, int) {
                     if(type & (SC_MOD_INSERTTEXT | SC_MOD_DELETETEXT))
                     {
                       m_Impl->moveHistoryForEdit(state, type, position, length);
                       m_Impl->markModified(state);
                     }
                   });
  QObject::connect(editor, &ScintillaEdit::charAdded, this, [this, state](int character) {
    if(!state->editor)
      return;
    if(character == '(' || character == ',')
    {
      m_Impl->callTip(state);
      return;
    }

    if(character == '.' || character == ':' || character == '>')
    {
      requestCompletion(state->editor.data(), false);
      return;
    }

    if(isIdentifierByte(char(character)))
    {
      const int caret = int(state->editor->currentPos());
      const QPair<int, int> prefixRange =
          identifierRange(state->editor->getText(state->editor->textLength() + 1), caret);
      if(prefixRange.second - prefixRange.first >= 2)
        requestCompletion(state->editor.data(), false);
    }
  });
  QObject::connect(editor, &ScintillaEdit::keyPressed, this,
                    [this, editor](QKeyEvent *event) { handleKeyPress(editor, event); });
  QObject::connect(editor, &ScintillaEdit::updateUi, this,
                   [this, state]() {
                     m_Impl->cancelMovedNavigation(state);
                     m_Impl->updateBraceHighlight(state);
                     m_Impl->updateSymbolHighlights(state);
                   });
  QObject::connect(editor, &ScintillaEdit::dwellStart, this,
                   [this, state](int x, int y) { m_Impl->showDiagnosticAt(state, x, y); });
  QObject::connect(editor, &ScintillaEdit::dwellEnd, this, [state](int, int) {
    if(state->editor && state->diagnosticTipActive)
    {
      state->editor->callTipCancel();
      state->diagnosticTipActive = false;
    }
  });

  m_Impl->sendDidOpen(state);
  m_Impl->updateBraceHighlight(state);
  m_Impl->updateSymbolHighlights(state);
  if(!m_Impl->serverAttempted && m_Impl->supportsSemanticLanguage(state))
    m_Impl->startServer(false);
}

void CodeEditorService::unregisterEditor(ScintillaEdit *editor)
{
  Impl::EditorState *state = m_Impl->states.take(editor);
  if(!state)
    return;

  // Cancel source and dependency requests before the editor address becomes dangling.
  m_Impl->cancelRequests(editor, true);

  m_Impl->sendDidClose(state);
  if(m_Impl->referencesMenu) m_Impl->referencesMenu->close();
  editor->removeEventFilter(this);
  QObject::disconnect(editor, NULL, this, NULL);
  for(QList<Impl::NavigationEntry> *history : {&m_Impl->backHistory, &m_Impl->forwardHistory})
    for(int i = history->size() - 1; i >= 0; --i)
      if((*history)[i].editor.data() == editor) history->removeAt(i);
  m_Impl->removeIndexedDocument(state);
  delete state;
}

bool CodeEditorService::isRegistered(ScintillaEdit *editor) const
{
  return m_Impl->states.contains(editor);
}

void CodeEditorService::setDefinitionHandler(const DefinitionHandler &handler)
{
  m_Impl->definitionHandler = handler;
}

void CodeEditorService::setDiagnosticsHandler(const DiagnosticsHandler &handler)
{
  m_Impl->diagnosticsHandler = handler;
  if(handler)
    for(Impl::EditorState *state : m_Impl->states)
      handler(state->editor.data(), m_Impl->combinedDiagnostics(state));
}

void CodeEditorService::setStatusHandler(const StatusHandler &handler)
{
  m_Impl->statusHandler = handler;
}

bool CodeEditorService::eventFilter(QObject *watched, QEvent *event)
{
  ScintillaEdit *editor = qobject_cast<ScintillaEdit *>(watched);
  Impl::EditorState *state = m_Impl->stateFor(editor);
  if(state && event && (event->type() == QEvent::PaletteChange ||
                        event->type() == QEvent::ApplicationPaletteChange ||
                        event->type() == QEvent::StyleChange))
  {
    m_Impl->configureIndicators(editor);
    state->semanticRequestedVersion = -1;
    state->semanticPaintGeneration = ++m_Impl->semanticPaintSerial;
    m_Impl->semanticTimer->start();
  }
  if(!state || !event || event->type() != QEvent::KeyPress)
    return QObject::eventFilter(watched, event);

  QKeyEvent *keyEvent = static_cast<QKeyEvent *>(event);
  const Qt::KeyboardModifiers modifiers = keyEvent->modifiers();
  Qt::KeyboardModifiers modifiersWithoutKeypad = modifiers & ~Qt::KeypadModifier;

  if(modifiersWithoutKeypad == Qt::AltModifier &&
     (keyEvent->key() == Qt::Key_Left || keyEvent->key() == Qt::Key_Right))
  {
    if(m_Impl->navigateHistory(editor, keyEvent->key() == Qt::Key_Left))
    {
      event->accept();
      return true;
    }
  }

  // Handle command keys before Scintilla sees them. Its keyPressed signal is emitted after its
  // normal key map has already run, which is too late for Ctrl+/ and the custom line operations.
  if(handleKeyPress(editor, keyEvent))
    return true;

  if(editor->readOnly())
    return QObject::eventFilter(watched, event);

  const bool primary = hasPrimaryModifier(modifiers, false);
  const bool primaryShift = (modifiersWithoutKeypad & Qt::ShiftModifier) &&
                            hasPrimaryModifier(modifiers, true);
  if(keyEvent->key() == Qt::Key_Backspace && modifiersWithoutKeypad == Qt::NoModifier)
  {
    if(m_Impl->deleteEmptyPair(state))
    {
      event->accept();
      return true;
    }
  }
  if((keyEvent->key() == Qt::Key_Return || keyEvent->key() == Qt::Key_Enter) && primary)
  {
    if(m_Impl->insertOpenLine(state, false))
    {
      event->accept();
      return true;
    }
  }
  if((keyEvent->key() == Qt::Key_Return || keyEvent->key() == Qt::Key_Enter) && primaryShift)
  {
    if(m_Impl->insertOpenLine(state, true))
    {
      event->accept();
      return true;
    }
  }
  if(keyEvent->key() == Qt::Key_Slash && primary)
  {
    if(m_Impl->toggleLineComment(state))
    {
      event->accept();
      return true;
    }
  }
  if((keyEvent->key() == Qt::Key_Tab || keyEvent->key() == Qt::Key_Backtab) &&
     !(modifiersWithoutKeypad & (Qt::ControlModifier | Qt::AltModifier | Qt::MetaModifier)))
  {
    const bool unindent = keyEvent->key() == Qt::Key_Backtab ||
                          (modifiersWithoutKeypad & Qt::ShiftModifier);
    if(m_Impl->indentSelectedLines(state, unindent))
    {
      event->accept();
      return true;
    }
  }
  if((keyEvent->key() == Qt::Key_Return || keyEvent->key() == Qt::Key_Enter) &&
     (modifiersWithoutKeypad == Qt::NoModifier ||
      modifiersWithoutKeypad == Qt::ShiftModifier))
  {
    if(m_Impl->insertSmartNewline(state))
    {
      event->accept();
      return true;
    }
  }

  if(!(modifiersWithoutKeypad &
       (Qt::ControlModifier | Qt::AltModifier | Qt::MetaModifier)))
  {
    const QString text = keyEvent->text();
    if(text.size() == 1 && text[0].unicode() <= 0x7f &&
       m_Impl->typePairedCharacter(state, char(text[0].unicode())))
    {
      event->accept();
      return true;
    }
  }

  return QObject::eventFilter(watched, event);
}

bool CodeEditorService::handleKeyPress(ScintillaEdit *editor, QKeyEvent *event)
{
  if(!editor || !event || !isRegistered(editor))
    return false;

  if(event->key() == Qt::Key_F12 && event->modifiers() == Qt::NoModifier)
  {
    requestDefinition(editor);
    event->accept();
    return true;
  }
  if(event->key() == Qt::Key_Space && (event->modifiers() & Qt::ControlModifier))
  {
    requestCompletion(editor, true);
    event->accept();
    return true;
  }
  return false;
}

void CodeEditorService::requestDefinition(ScintillaEdit *editor, int position)
{
  Impl::EditorState *state = m_Impl->stateFor(editor);
  if(!state)
    return;
  m_Impl->syncText(state, false);
  if(position < 0)
    position = int(editor->currentPos());
  if(m_Impl->requestLspNavigation(state, position, false)) return;
  m_Impl->reportStatus(QCoreApplication::translate("CodeEditorService",
      "Semantic language server is unavailable; using limited local navigation."));
  const Location fallback = m_Impl->localDefinition(state, position);
  const auto range = identifierRange(state->text, position);
  if(fallback.editor == editor && fallback.position == range.first)
  {
    m_Impl->showReferences(state, position);
    return;
  }
  if(fallback.editor)
    m_Impl->navigateToDefinition(fallback, editor, position);
  else
    m_Impl->reportStatus(QCoreApplication::translate(
        "CodeEditorService", "No definition was found for the selected symbol"));
}

void CodeEditorService::requestReferences(ScintillaEdit *editor, int position)
{
  if(!editor) return;
  if(position < 0) position = int(editor->currentPos());
  Impl::EditorState *state = m_Impl->stateFor(editor);
  if(m_Impl->requestLspNavigation(state, position, true)) return;
  m_Impl->reportStatus(QCoreApplication::translate("CodeEditorService",
      "Semantic language server is unavailable; using limited local navigation."));
  m_Impl->showReferences(state, position);
}

void CodeEditorService::rememberNavigation(ScintillaEdit *editor, int targetPosition)
{
  if(!editor || m_Impl->deliveringNavigation || targetPosition == editor->currentPos()) return;
  m_Impl->pushHistory(m_Impl->backHistory, m_Impl->navigationEntry(editor));
  m_Impl->forwardHistory.clear();
}

void CodeEditorService::clearNavigation(ScintillaEdit *editor)
{
  for(QList<Impl::NavigationEntry> *history : {&m_Impl->backHistory, &m_Impl->forwardHistory})
    for(int i = history->size() - 1; i >= 0; --i)
      if((*history)[i].editor.data() == editor) history->removeAt(i);
}

bool CodeEditorService::navigateHistory(ScintillaEdit *editor, bool backwards)
{
  return m_Impl->navigateHistory(editor, backwards);
}

bool CodeEditorService::canNavigateHistory(bool backwards) const
{
  return !(backwards ? m_Impl->backHistory : m_Impl->forwardHistory).isEmpty();
}

void CodeEditorService::requestCompletion(ScintillaEdit *editor, bool explicitRequest)
{
  Impl::EditorState *state = m_Impl->stateFor(editor);
  if(!state)
    return;
  m_Impl->syncText(state, false);
  const int caret = int(editor->currentPos());
  const QPair<int, int> range = identifierRange(state->text, caret);
  const QString prefix = QString::fromUtf8(state->text.mid(range.first, caret - range.first));
  if(!explicitRequest && prefix.size() < 2 && caret > 0 && state->text[caret - 1] != '.' &&
     state->text[caret - 1] != ':' && state->text[caret - 1] != '>')
    return;
  m_Impl->showCompletions(state, prefix);
  m_Impl->requestLspCompletion(state, prefix);
}

void CodeEditorService::showCallTip(ScintillaEdit *editor)
{
  m_Impl->callTip(m_Impl->stateFor(editor));
}

CodeEditorService::Location CodeEditorService::findLocalDefinition(ScintillaEdit *editor,
                                                                   int position) const
{
  return m_Impl->localDefinition(m_Impl->stateFor(editor), position);
}

QString CodeEditorService::documentSourceSha256(ScintillaEdit *editor)
{
  Impl::EditorState *state = m_Impl->stateFor(editor);
  if(!state) return QString();
  m_Impl->syncText(state, false);
  return state->sourceSha256;
}

QList<CodeEditorService::SymbolInfo> CodeEditorService::symbolTable(ScintillaEdit *editor)
{
  QList<SymbolInfo> result;
  Impl::EditorState *state = m_Impl->stateFor(editor);
  if(!state || !state->editor) return result;
  m_Impl->syncText(state, false);
  result.reserve(state->symbols.size());
  for(const Impl::Symbol &symbol : state->symbols)
  {
    SymbolInfo info;
    info.name = symbol.name;
    info.valueType = symbol.valueType;
    info.ownerType = symbol.ownerType;
    info.sourceSha256 = state->sourceSha256;
    info.declaration.editor = editor;
    info.declaration.fileName = state->fileName;
    info.declaration.position = symbol.position;
    info.declaration.line = int(editor->lineFromPosition(symbol.position));
    info.declaration.column = symbol.position - int(editor->positionFromLine(info.declaration.line));
    info.scopeStart = symbol.scopeStart > 0 && symbol.signature.isEmpty()
                          ? qMax(symbol.scopeStart, symbol.position) : symbol.scopeStart;
    info.scopeEnd = symbol.scopeEnd;
    info.function = !symbol.signature.isEmpty();
    info.global = symbol.scopeStart == 0 && symbol.ownerType.isEmpty();
    result.push_back(info);
  }
  std::sort(result.begin(), result.end(), [](const SymbolInfo &a, const SymbolInfo &b) {
    return a.declaration.position < b.declaration.position;
  });
  return result;
}

CodeEditorService::SymbolInspection CodeEditorService::inspectSymbol(ScintillaEdit *editor,
                                                                     int position,
                                                                     int maxReferences)
{
  SymbolInspection result;
  Impl::EditorState *state = m_Impl->stateFor(editor);
  if(!state || !state->editor) return result;
  const Location definition = m_Impl->localDefinition(state, position);
  // Open editors can represent different shaders with coincident names. Keep this lightweight
  // inspection tied to one exact source revision; cross-file semantics belong to the Host/LSP.
  if(definition.editor != editor) return result;
  for(const SymbolInfo &symbol : symbolTable(editor))
    if(symbol.declaration.position == definition.position)
    {
      result.symbol = symbol;
      break;
    }
  if(!result.isValid()) return result;

  const QByteArray needle = result.symbol.name.toUtf8();
  const QByteArray masked = maskedCode(state->text, state->language);
  const int limit = qBound(1, maxReferences, 256);
  for(int offset = 0; (offset = masked.indexOf(needle, offset)) >= 0;)
  {
    const int found = offset;
    offset += needle.size();
    if((found > 0 && isIdentifierByte(masked[found - 1])) ||
       (offset < masked.size() && isIdentifierByte(masked[offset])) ||
       found == definition.position)
      continue;
    const Location resolved = m_Impl->resolveDefinition(state, found, result.symbol.name, 0,
                                                       &masked);
    if(resolved.editor != editor || resolved.position != definition.position) continue;
    if(result.references.size() >= limit)
    {
      result.referencesTruncated = true;
      break;
    }
    Location reference;
    reference.editor = editor;
    reference.fileName = state->fileName;
    reference.position = found;
    reference.line = int(editor->lineFromPosition(found));
    reference.column = found - int(editor->positionFromLine(reference.line));
    result.references.push_back(reference);
  }
  return result;
}

QList<CodeEditorService::Diagnostic> CodeEditorService::diagnostics(ScintillaEdit *editor) const
{
  Impl::EditorState *state = m_Impl->stateFor(editor);
  if(!state)
    return QList<Diagnostic>();
  m_Impl->syncText(state, true);
  return m_Impl->combinedDiagnostics(state);
}

void CodeEditorService::setExternalDiagnostics(ScintillaEdit *editor,
                                               const QList<Diagnostic> &diagnostics)
{
  Impl::EditorState *state = m_Impl->stateFor(editor);
  if(!state || !state->editor)
    return;

  state->externalDiagnostics.clear();
  const int documentLength = int(editor->length());
  const int count = qMin(diagnostics.size(), MaxDiagnostics);
  state->externalDiagnostics.reserve(count);
  for(int i = 0; i < count; i++)
  {
    Diagnostic diagnostic = diagnostics[i];
    diagnostic.start = qBound(0, diagnostic.start, documentLength);
    diagnostic.length = qMax(1, qMin(diagnostic.length, documentLength - diagnostic.start));
    diagnostic.line = int(editor->lineFromPosition(diagnostic.start));
    diagnostic.column = diagnostic.start - int(editor->positionFromLine(diagnostic.line));
    diagnostic.message = diagnostic.message.left(2048);
    diagnostic.source = diagnostic.source.left(128);
    state->externalDiagnostics.push_back(diagnostic);
  }
  m_Impl->displayDiagnostics(state);
}

void CodeEditorService::setLanguageServerCommand(const QString &program, const QStringList &arguments)
{
  stopLanguageServer();
  m_Impl->configuredProgram = program;
  m_Impl->resolvedProgram.clear();
  m_Impl->serverArguments = arguments;
  m_Impl->serverAttempted = false;
}

bool CodeEditorService::startLanguageServer()
{
  return m_Impl->startServer(true);
}

void CodeEditorService::stopLanguageServer()
{
  m_Impl->queuedNavigation.clear();
  m_Impl->referenceSearches.clear();
  m_Impl->referenceCache.clear();
  if(m_Impl->process->state() == QProcess::NotRunning)
    return;
  m_Impl->stopping = true;
  m_Impl->serverReady = false;
  m_Impl->pending.clear();
  for(Impl::EditorState *state : m_Impl->states)
    state->openSent = false;
  m_Impl->process->terminate();
  if(!m_Impl->process->waitForFinished(150))
  {
    m_Impl->process->kill();
    m_Impl->process->waitForFinished(150);
  }
}

QString CodeEditorService::languageServerProgram() const
{
  return m_Impl->findServerProgram();
}

bool CodeEditorService::languageServerRunning() const
{
  return m_Impl->serverReady && m_Impl->process->state() == QProcess::Running;
}

#if ENABLE_UNIT_TESTS

#include "3rdparty/catch/catch.hpp"

struct CodeEditorServiceTestAccess
{
  static bool glslUnsupportedProviderReportsDoNotHideRealErrors()
  {
    CodeEditorService service;
    CodeEditorService::Impl::EditorState state;
    state.language = QStringLiteral("glsl");
    CodeEditorService::Diagnostic local, compiler, unsupported;
    local.message = QStringLiteral("Unmatched closing delimiter");
    compiler.message = QStringLiteral("glslang: unknown identifier BreadMissingSymbol");
    unsupported.message = QStringLiteral("GLSL layout qualifier is unrecognized");
    state.localDiagnostics.push_back(local);
    state.externalDiagnostics.push_back(compiler);
    state.lspDiagnostics.push_back(unsupported);
    service.m_Impl->process->setProgram(QStringLiteral("C:/Bread/SLANGD.exe"));
    const auto glsl = service.m_Impl->combinedDiagnostics(&state);
    const bool realErrorsRetained = glsl.size() == 2 && glsl[0].message == local.message &&
                                    glsl[1].message == compiler.message;
    state.language = QStringLiteral("hlsl");
    const auto hlsl = service.m_Impl->combinedDiagnostics(&state);
    const bool hlslReportsRetained = hlsl.size() == 3 && hlsl[1].message == unsupported.message;
    state.language = QStringLiteral("glsl");
    service.m_Impl->process->setProgram(QStringLiteral("C:/Bread/shader-language-server.exe"));
    const auto nativeGLSL = service.m_Impl->combinedDiagnostics(&state);
    return realErrorsRetained && hlslReportsRetained && nativeGLSL.size() == 3 &&
           nativeGLSL[1].message == unsupported.message;
  }

  static bool semanticRequestsSurviveNavigationButNotEdits()
  {
    CodeEditorService service;
    // Only the opaque identity is used by cancellation; no widget or QApplication is needed.
    ScintillaEdit *identity = reinterpret_cast<ScintillaEdit *>(&service);
    CodeEditorService::Impl::PendingRequest semantic, navigation;
    semantic.kind = CodeEditorService::Impl::PendingKind::SemanticTokens;
    semantic.sourceEditor = identity;
    navigation.kind = CodeEditorService::Impl::PendingKind::Definition;
    navigation.sourceEditor = identity;
    service.m_Impl->pending[1] = semantic;
    service.m_Impl->pending[2] = navigation;
    service.m_Impl->cancelRequests(identity, false);
    const bool onlyColoringRemains = service.m_Impl->pending.size() == 1 &&
                                    service.m_Impl->pending.contains(1);
    service.m_Impl->cancelRequests(identity, true);
    return onlyColoringRemains && service.m_Impl->pending.isEmpty();
  }

  static bool semanticIdentityUsesFileAndCompleteRange()
  {
    CodeEditorService service;
    auto location = [](const QString &path, int endCharacter) {
      QJsonObject start, end, range, result;
      start[QStringLiteral("line")] = 3;
      start[QStringLiteral("character")] = 5;
      end[QStringLiteral("line")] = 3;
      end[QStringLiteral("character")] = endCharacter;
      range[QStringLiteral("start")] = start;
      range[QStringLiteral("end")] = end;
      result[QStringLiteral("uri")] = QUrl::fromLocalFile(path).toString();
      result[QStringLiteral("range")] = range;
      return result;
    };
    const QString root = QDir::tempPath();
    const auto first = location(root + QStringLiteral("/first/shader.hlsl"), 10);
    const auto second = location(root + QStringLiteral("/second/shader.hlsl"), 10);
    const auto differentRange = location(root + QStringLiteral("/first/shader.hlsl"), 11);
    QJsonObject link;
    link[QStringLiteral("targetUri")] = first.value(QStringLiteral("uri"));
    link[QStringLiteral("targetSelectionRange")] = first.value(QStringLiteral("range"));
    return !service.m_Impl->semanticLocationKey(first).isEmpty() &&
           service.m_Impl->semanticLocationKey(first) != service.m_Impl->semanticLocationKey(second) &&
           service.m_Impl->semanticLocationKey(first) != service.m_Impl->semanticLocationKey(differentRange) &&
           service.m_Impl->semanticLocationKey(first) == service.m_Impl->semanticLocationKey(link) &&
           service.m_Impl->semanticLocationKey(QJsonValue()).isEmpty();
  }

  static bool incrementalIndexPreservesOtherDocuments()
  {
    CodeEditorService service;
    CodeEditorService::Impl::EditorState edited;
    CodeEditorService::Impl::EditorState untouched;
    edited.language = QStringLiteral("hlsl");
    untouched.language = QStringLiteral("hlsl");
    edited.text = "float before;";
    untouched.text = "float retained;";
    service.m_Impl->reindexDocument(&edited);
    service.m_Impl->reindexDocument(&untouched);
    const bool initial = service.m_Impl->symbolIndex.value(QStringLiteral("before")).size() == 1 &&
                         service.m_Impl->symbolIndex.value(QStringLiteral("retained")).size() == 1;
    edited.text = "float after;";
    service.m_Impl->reindexDocument(&edited);
    const bool updated = !service.m_Impl->symbolIndex.contains(QStringLiteral("before")) &&
                         service.m_Impl->symbolIndex.value(QStringLiteral("after")).size() == 1 &&
                         service.m_Impl->symbolIndex.value(QStringLiteral("retained")).size() == 1 &&
                         service.m_Impl->symbolIndex.value(QStringLiteral("retained"))[0].state ==
                             &untouched;
    service.m_Impl->removeIndexedDocument(&edited);
    const bool removed = !service.m_Impl->symbolIndex.contains(QStringLiteral("after")) &&
                         service.m_Impl->symbolIndex.value(QStringLiteral("retained")).size() == 1;
    service.m_Impl->removeIndexedDocument(&untouched);
    return initial && updated && removed && service.m_Impl->symbolIndex.isEmpty();
  }
};

TEST_CASE("GLSL partial semantic providers retain target compiler and local errors", "[code-editor]")
{
  CHECK(CodeEditorServiceTestAccess::glslUnsupportedProviderReportsDoNotHideRealErrors());
}

TEST_CASE("Code editor reindexes only the changed source document", "[code-editor]")
{
  CHECK(CodeEditorServiceTestAccess::incrementalIndexPreservesOtherDocuments());
}

TEST_CASE("Incremental language servers replace the previous synchronized document", "[code-editor]")
{
  const QByteArray previous("// \xE6\xB3\x95\xE7\xBA\xBF \xF0\x9F\x98\x80\r\n"
                            "float oldValue; // \xE5\xB0\xBE \xF0\x9F\x98\x80");
  const QByteArray current("struct NewType { float value; };\n");
  const QJsonObject incremental = lspContentChange(2, previous, current);
  const QJsonObject range = incremental.value(QStringLiteral("range")).toObject();
  CHECK(range.value(QStringLiteral("start")).toObject().value(QStringLiteral("line")).toInt() == 0);
  CHECK(range.value(QStringLiteral("start")).toObject().value(QStringLiteral("character")).toInt() == 0);
  CHECK(range.value(QStringLiteral("end")).toObject().value(QStringLiteral("line")).toInt() == 1);
  CHECK(range.value(QStringLiteral("end")).toObject().value(QStringLiteral("character")).toInt() == 23);
  CHECK(incremental.value(QStringLiteral("text")).toString().toUtf8() == current);
  CHECK(lspContentChange(1, previous, current).contains(QStringLiteral("range")) == false);
  CHECK(lspContentChange(2, QByteArray(), current).value(QStringLiteral("range")).toObject()
            .value(QStringLiteral("end")).toObject().value(QStringLiteral("character")).toInt() == 0);
  CHECK(lspContentChange(2, current, QByteArray()).value(QStringLiteral("range")).toObject()
            .value(QStringLiteral("end")).toObject().value(QStringLiteral("line")).toInt() == 1);
  CHECK(slangNeedsWholeDocumentReopen(previous));
  CHECK_FALSE(slangNeedsWholeDocumentReopen(previous + '\n'));
  CHECK_FALSE(slangNeedsWholeDocumentReopen(current));
  CHECK_FALSE(slangNeedsWholeDocumentReopen(QByteArray("float value;")));
}

TEST_CASE("Semantic coloring is independent of caret navigation and cancelled by edits", "[code-editor]")
{
  CHECK(CodeEditorServiceTestAccess::semanticRequestsSurviveNavigationButNotEdits());
}

TEST_CASE("Shader semantic token colors use compiler categories and UTF-16 ranges", "[code-editor]")
{
  const QStringList legend = {QStringLiteral("type"), QStringLiteral("variable"),
                             QStringLiteral("parameter"), QStringLiteral("function"),
                             QStringLiteral("property"), QStringLiteral("keyword"),
                             QStringLiteral("string")};
  QVector<SemanticColorRange> ranges;
  SECTION("function, type, parameter, field and variable receive distinct colors")
  {
    const QByteArray code("Light Shade(Light input) {\n  float value = input.power;\n}\n");
    const QJsonArray data = {0, 0, 5, 0, 0,  0, 6, 5, 3, 0,
                            0, 6, 5, 0, 0,  0, 6, 5, 2, 0,
                            1, 8, 5, 1, 0,  0, 8, 5, 2, 0,
                            0, 6, 5, 4, 0};
    REQUIRE(decodeSemanticColorRanges(code, data, legend, true, ranges));
    REQUIRE(ranges.size() == 7);
    CHECK(code.mid(ranges[0].start, ranges[0].length) == QByteArray("Light"));
    CHECK(code.mid(ranges[1].start, ranges[1].length) == QByteArray("Shade"));
    CHECK(code.mid(ranges[4].start, ranges[4].length) == QByteArray("value"));
    CHECK(code.mid(ranges[6].start, ranges[6].length) == QByteArray("power"));
    CHECK(ranges[0].color != ranges[1].color);
    CHECK(ranges[3].color != ranges[4].color);
    CHECK(ranges[4].color != ranges[6].color);
  }
  SECTION("Chinese comments and surrogate pairs preserve exact UTF-8 byte positions")
  {
    const QByteArray code("/* \xE6\xB3\x95\xE7\xBA\xBF \xF0\x9F\x98\x80 */ "
                          "float value;\r\nfloat other;\n");
    const QString source = QString::fromUtf8(code);
    const int column = source.indexOf(QStringLiteral("value"));
    const QJsonArray data = {0, column, 5, 1, 0, 1, 6, 5, 1, 0};
    REQUIRE(decodeSemanticColorRanges(code, data, legend, false, ranges));
    REQUIRE(ranges.size() == 2);
    CHECK(ranges[0].start == code.indexOf("value"));
    CHECK(ranges[1].start == code.indexOf("other"));
  }
  SECTION("ignored lexical tokens do not corrupt relocated include coloring")
  {
    const QByteArray code("#include \"x.hlsli\"\nfloat value;\n");
    const QJsonArray data = {0, 0, 8, 5, 0, 0, 9, 100, 6, 0, 1, 6, 5, 1, 0};
    REQUIRE(decodeSemanticColorRanges(code, data, legend, true, ranges));
    REQUIRE(ranges.size() == 1);
    CHECK(ranges[0].start == code.indexOf("value"));
  }
  SECTION("malformed, overlapping and out of range tokens never paint")
  {
    const QByteArray code("float value;\n");
    CHECK_FALSE(decodeSemanticColorRanges(code, QJsonArray{0, 6, 5, 1}, legend, true, ranges));
    CHECK_FALSE(decodeSemanticColorRanges(code, QJsonArray{0, 6, 50, 1, 0}, legend, true, ranges));
    CHECK_FALSE(decodeSemanticColorRanges(code, QJsonArray{0, -1, 5, 1, 0}, legend, true, ranges));
    CHECK_FALSE(decodeSemanticColorRanges(code, QJsonArray{0, 6.5, 5, 1, 0}, legend, true, ranges));
    CHECK_FALSE(decodeSemanticColorRanges(code, QJsonArray{0, 6, 5, 30, 0}, legend, true, ranges));
    CHECK_FALSE(decodeSemanticColorRanges(
        code, QJsonArray{0, 6, 5, 1, 0, 0, 1, 3, 1, 0}, legend, true, ranges));
  }
}

TEST_CASE("Semantic navigation identity includes URI and full declaration range", "[code-editor]")
{
  CHECK(CodeEditorServiceTestAccess::semanticIdentityUsesFileAndCompleteRange());
}

TEST_CASE("Code editor smart newline planning", "[code-editor]")
{
  SECTION("an empty brace pair is split onto three lines")
  {
    const QByteArray text("if(enabled) {}");
    const int caret = text.indexOf('{') + 1;
    const SmartNewlineEdit edit =
        planSmartNewline(text, caret, caret, QByteArray("\n"), QByteArray("  "), false);
    CHECK(edit.replacement == QByteArray("\n  \n"));
    CHECK(edit.caretOffset == 3);
  }

  SECTION("the current indentation and editor EOL are preserved")
  {
    const QByteArray text("\tif(enabled) {");
    const SmartNewlineEdit edit = planSmartNewline(
        text, text.size(), text.size(), QByteArray("\r\n"), QByteArray("\t"), false);
    CHECK(edit.replacement == QByteArray("\r\n\t\t"));
    CHECK(edit.caretOffset == edit.replacement.size());
  }

  SECTION("python suites indent after a colon")
  {
    const QByteArray text("def render():");
    const SmartNewlineEdit edit = planSmartNewline(
        text, text.size(), text.size(), QByteArray("\n"), QByteArray("    "), true);
    CHECK(edit.replacement == QByteArray("\n    "));
  }
}

TEST_CASE("Code editor position transforms follow line comment edits", "[code-editor]")
{
  QList<TextReplacement> edits;
  edits.push_back({0, 0, QByteArray("// ")});
  edits.push_back({8, 0, QByteArray("// ")});
  CHECK(transformedPosition(0, edits) == 3);
  CHECK(transformedPosition(5, edits) == 8);
  CHECK(transformedPosition(10, edits) == 16);
}

TEST_CASE("Code editor indexes every comma declarator without indexing initializer arguments", "[code-editor]")
{
  SECTION("later local declarations retain their own source positions")
  {
    const QByteArray code("float value; float G(){ float first=0, value=1; return value; }");
    const auto declarations = followingDeclarators(
        code, codeDelimiterPairs(code), code.indexOf("first") + 5, QStringLiteral("float"));
    REQUIRE(declarations.size() == 1);
    CHECK(declarations[0].name.toUtf8() == QByteArray("value"));
    CHECK(declarations[0].position == code.indexOf("value=1"));
    CHECK(declarations[0].valueType.toUtf8() == QByteArray("float"));
  }
  SECTION("nested calls, indices and aggregates do not create declarations")
  {
    const QByteArray code("float first=Make(1, 2), values[2]={3, 4}, last=values[Index(0, 1)];");
    const auto declarations = followingDeclarators(
        code, codeDelimiterPairs(code), code.indexOf("first") + 5, QStringLiteral("float"));
    REQUIRE(declarations.size() == 2);
    CHECK(declarations[0].name.toUtf8() == QByteArray("values"));
    CHECK(declarations[1].name.toUtf8() == QByteArray("last"));
  }
  SECTION("the next typed function parameter is not a comma variable")
  {
    const QByteArray code("float F(float first, float value) { return value; }");
    CHECK(followingDeclarators(code, codeDelimiterPairs(code), code.indexOf("first") + 5,
                              QStringLiteral("float")).isEmpty());
  }
  SECTION("for initializers stop before the loop condition")
  {
    const QByteArray code("for(int first=0, value=1; Check(first, value); ++value) {}");
    const auto declarations = followingDeclarators(
        code, codeDelimiterPairs(code), code.indexOf("first") + 5, QStringLiteral("int"));
    REQUIRE(declarations.size() == 1);
    CHECK(declarations[0].name.toUtf8() == QByteArray("value"));
  }
}

#endif
