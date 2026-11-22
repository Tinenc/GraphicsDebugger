#pragma once

#include <QApplication>
#include <QPalette>
#include <QStringList>

// A bounded lexical formatter shared by transcript fences and saved-source cards.
// i18n-ignore: code keywords, token classes and CSS colours are syntax identifiers, not UI prose.
namespace AICodeFormatting
{
inline bool sourceKeyword(const QString &word)
{
  static const QStringList keywords =
      QStringList() << QLatin1String("auto") << QLatin1String("break") << QLatin1String("case")
                    << QLatin1String("class") << QLatin1String("const") << QLatin1String("continue")
                    << QLatin1String("discard") << QLatin1String("do") << QLatin1String("else")
                    << QLatin1String("enum") << QLatin1String("false") << QLatin1String("for")
                    << QLatin1String("if") << QLatin1String("in") << QLatin1String("interface")
                    << QLatin1String("inout") << QLatin1String("precise")
                    << QLatin1String("nointerpolation") << QLatin1String("centroid")
                    << QLatin1String("namespace") << QLatin1String("null")
                    << QLatin1String("nullptr") << QLatin1String("out") << QLatin1String("register")
                    << QLatin1String("return") << QLatin1String("static") << QLatin1String("struct")
                    << QLatin1String("switch") << QLatin1String("true") << QLatin1String("typedef")
                    << QLatin1String("uniform") << QLatin1String("using") << QLatin1String("var")
                    << QLatin1String("while") << QLatin1String("class") << QLatin1String("def")
                    << QLatin1String("import") << QLatin1String("from") << QLatin1String("as")
                    << QLatin1String("async") << QLatin1String("await") << QLatin1String("public")
                    << QLatin1String("private") << QLatin1String("protected")
                    << QLatin1String("override") << QLatin1String("virtual") << QLatin1String("new")
                    << QLatin1String("let") << QLatin1String("function") << QLatin1String("yield")
                    << QLatin1String("try") << QLatin1String("except") << QLatin1String("catch")
                    << QLatin1String("finally") << QLatin1String("throw") << QLatin1String("None")
                    << QLatin1String("True") << QLatin1String("False") << QLatin1String("pass")
                    << QLatin1String("and") << QLatin1String("or") << QLatin1String("not")
                    << QLatin1String("is");
  return keywords.contains(word);
}

inline bool sourceType(const QString &word)
{
  static const QStringList types =
      QStringList() << QLatin1String("bool") << QLatin1String("buffer") << QLatin1String("byte")
                    << QLatin1String("cbuffer") << QLatin1String("char") << QLatin1String("double")
                    << QLatin1String("fixed") << QLatin1String("float") << QLatin1String("half")
                    << QLatin1String("int") << QLatin1String("long") << QLatin1String("matrix")
                    << QLatin1String("object") << QLatin1String("short") << QLatin1String("size_t")
                    << QLatin1String("string") << QLatin1String("uint") << QLatin1String("ulong")
                    << QLatin1String("ushort") << QLatin1String("void");
  if(types.contains(word))
    return true;

  // Cover shader vector/matrix spellings (float4, half3x3, ivec2, mat4, etc.) without
  // accepting arbitrary identifiers that merely start with a primitive type name.
  static const QStringList numericPrefixes =
      QStringList() << QLatin1String("float") << QLatin1String("half") << QLatin1String("double")
                    << QLatin1String("fixed") << QLatin1String("uint") << QLatin1String("int")
                    << QLatin1String("bool") << QLatin1String("vec") << QLatin1String("ivec")
                    << QLatin1String("uvec") << QLatin1String("bvec") << QLatin1String("dvec")
                    << QLatin1String("mat") << QLatin1String("dmat");
  for(const QString &prefix : numericPrefixes)
  {
    if(word.startsWith(prefix) && word.size() > prefix.size() && word[prefix.size()].isDigit())
      return true;
  }

  // Rider treats named types as a distinct semantic category. An initial upper-case character
  // gives useful, language-agnostic coverage for C++, C#, Java and shader resource types.
  return !word.isEmpty() && word[0].isUpper();
}

inline QString lexicalSourceLine(const QString &line, const QString &language, bool &blockComment)
{
  const QString loweredLanguage = language.toLower();
  const bool hashComment =
      loweredLanguage == QLatin1String("python") || loweredLanguage == QLatin1String("py") ||
      loweredLanguage == QLatin1String("bash") || loweredLanguage == QLatin1String("shell") ||
      loweredLanguage == QLatin1String("sh") || loweredLanguage == QLatin1String("powershell") ||
      loweredLanguage == QLatin1String("ps1");
  QString html;
  int i = 0;
  while(i < line.size())
  {
    if(blockComment)
    {
      const int end = line.indexOf(QLatin1String("*/"), i);
      const int length = end < 0 ? line.size() - i : end + 2 - i;
      html += QLatin1String("<span class=\"source-comment\">") +
              line.mid(i, length).toHtmlEscaped() + QLatin1String("</span>");
      blockComment = end < 0;
      i += length;
      continue;
    }
    if(i + 1 < line.size() && line[i] == QLatin1Char('/') && line[i + 1] == QLatin1Char('/'))
    {
      html += QLatin1String("<span class=\"source-comment\">") + line.mid(i).toHtmlEscaped() +
              QLatin1String("</span>");
      break;
    }
    if(i + 1 < line.size() && line[i] == QLatin1Char('/') && line[i + 1] == QLatin1Char('*'))
    {
      const int end = line.indexOf(QLatin1String("*/"), i + 2);
      blockComment = end < 0;
      const int length = end < 0 ? line.size() - i : end + 2 - i;
      html += QLatin1String("<span class=\"source-comment\">") +
              line.mid(i, length).toHtmlEscaped() + QLatin1String("</span>");
      i += length;
      continue;
    }
    if(hashComment && line[i] == QLatin1Char('#'))
    {
      html += QLatin1String("<span class=\"source-comment\">") + line.mid(i).toHtmlEscaped() +
              QLatin1String("</span>");
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
                                   ? QLatin1String("source-key")
                                   : QLatin1String("source-string");
      html += QLatin1String("<span class=\"") + cssClass + QLatin1String("\">") +
              line.mid(start, i - start).toHtmlEscaped() + QLatin1String("</span>");
      continue;
    }
    if(line[i].isLetter() || line[i] == QLatin1Char('_'))
    {
      const int start = i++;
      while(i < line.size() && (line[i].isLetterOrNumber() || line[i] == QLatin1Char('_')))
        ++i;
      const QString word = line.mid(start, i - start);
      int next = i;
      while(next < line.size() && line[next].isSpace())
        ++next;
      if(sourceKeyword(word))
        html += QLatin1String("<span class=\"source-keyword\">") + word.toHtmlEscaped() +
                QLatin1String("</span>");
      else if(next < line.size() && line[next] == QLatin1Char(':'))
        html += QLatin1String("<span class=\"source-key\">") + word.toHtmlEscaped() +
                QLatin1String("</span>");
      else if(next < line.size() && line[next] == QLatin1Char('('))
        html += QLatin1String("<span class=\"source-function\">") + word.toHtmlEscaped() +
                QLatin1String("</span>");
      else if(sourceType(word))
        html += QLatin1String("<span class=\"source-type\">") + word.toHtmlEscaped() +
                QLatin1String("</span>");
      else
        html += QLatin1String("<span class=\"source-variable\">") + word.toHtmlEscaped() +
                QLatin1String("</span>");
      continue;
    }
    if(line[i].isDigit() ||
       (line[i] == QLatin1Char('.') && i + 1 < line.size() && line[i + 1].isDigit()))
    {
      const int start = i++;
      while(i < line.size() &&
            (line[i].isLetterOrNumber() || line[i] == QLatin1Char('.') ||
             ((line[i] == QLatin1Char('+') || line[i] == QLatin1Char('-')) && i > start &&
              (line[i - 1].toLower() == QLatin1Char('e') ||
               line[i - 1].toLower() == QLatin1Char('p')))))
        ++i;
      html += QLatin1String("<span class=\"source-number\">") +
              line.mid(start, i - start).toHtmlEscaped() + QLatin1String("</span>");
      continue;
    }
    html += QString(line[i]).toHtmlEscaped();
    ++i;
  }
  return html;
}

inline QString highlightedSourceLine(const QString &line, const QString &language, bool &blockComment,
                                     const QPalette &palette = QApplication::palette())
{
  QString html = lexicalSourceLine(line, language, blockComment);
  const bool dark = palette.color(QPalette::Base).lightness() < 128;
  const char *roles[] = {"keyword", "type",   "function", "variable",
                         "string",  "number", "comment",  "key"};
  const char *darkColors[] = {"#cc7832", "#b8d7a3", "#ffc66d", "#a9b7c6",
                              "#6a8759", "#6897bb", "#808080", "#9876aa"};
  const char *lightColors[] = {"#0033b3", "#871094", "#00627a", "#202020",
                               "#067d17", "#1750eb", "#6a737d", "#9e5b00"};
  // Inline colour survives both a QTextDocument stylesheet and an enclosing source hyperlink.
  for(int i = 0; i < 8; i++)
  {
    const QString token =
        QLatin1String("<span class=\"source-") + QLatin1String(roles[i]) + QLatin1String("\"");
    html.replace(token + QLatin1Char('>'),
                 token + QLatin1String(" style=\"color:") +
                     QLatin1String(dark ? darkColors[i] : lightColors[i]) + QLatin1String(";\">"));
  }
  return html;
}
}
