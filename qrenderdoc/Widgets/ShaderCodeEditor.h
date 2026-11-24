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

#pragma once

#include <QWidget>
#include <QByteArray>
#include <QSet>
#include <QMap>
#include <QVector>
#include "Code/Interface/QRDInterface.h"

class CodeEditorService;
class FindReplace;
class QKeyEvent;
class QPoint;
class ScintillaEdit;
class QToolButton;

// The single embeddable shader editing surface used by first-party workspaces. It deliberately
// owns no file/save/compile/replay policy: those operations are callbacks to the workspace owner.
class ShaderCodeEditor : public QWidget, public IShaderCodeEditor
{
  Q_OBJECT

public:
  explicit ShaderCodeEditor(CodeEditorService *services, QWidget *parent = 0);
  ~ShaderCodeEditor() override;

  QWidget *Widget() override { return this; }
  rdcstr GetText() override;
  void SetText(const rdcstr &text) override;
  void SetReadOnly(bool readOnly) override;
  void SetNavigationEnabled(bool enabled) override;
  bool IsReadOnly() override;
  void SetFileName(const rdcstr &fileName) override;
  void SetIncludeBaseDirectory(const rdcstr &directory) override;
  void SetLanguage(const rdcstr &language) override;
  bool IsModified() override;
  void SetModified(bool modified) override;
  void SetSavePoint() override;
  void GoToLine(uint32_t line, uint32_t column = 1) override;
  void SelectRange(uint32_t byteStart, uint32_t byteEnd) override;
  uint32_t GetCursorLine() override;
  uint32_t GetCursorColumn() override;
  int32_t CreateLineAnchor(uint32_t line) override;
  uint32_t GetLineAnchorLine(int32_t anchor) override;
  void RemoveLineAnchor(int32_t anchor) override;
  void ClearLineAnchors() override;
  int32_t CreateRangeAnchor(uint32_t byteStart, uint32_t byteEnd) override;
  rdcarray<uint32_t> GetRangeAnchorRange(int32_t anchor) override;
  void RemoveRangeAnchor(int32_t anchor) override;
  void SetDiagnostics(const rdcarray<ShaderEditorDiagnostic> &diagnostics) override;
  void SetTextChangedCallback(EventCallback callback) override;
  void SetSaveCallback(EventCallback callback) override;
  void SetAddToChatCallback(SelectionCallback callback) override;
  void SetDefinitionCallback(DefinitionCallback callback) override;
  void SetVisualDebugCallback(VisualDebugCallback callback) override;
  void SetStopVisualDebugCallback(EventCallback callback) override;
  void RequestVisualDebug() override;
  void SetVisualDebugRange(uint32_t byteStart, uint32_t byteEnd, bool active) override;
  void ClearCallbacks() override;

  ScintillaEdit *Editor() const { return m_Editor; }
  void GoToPosition(int position);
  void OpenDefinition(const QString &fileName, int line, int column);
  void SetStatusMessage(const QString &message);
  void SetDiagnosticSummary(int errors, int warnings, const QString &details, int firstLine);

protected:
  bool eventFilter(QObject *watched, QEvent *event) override;

private:
  void RegisterDocument();
  void UpdateLineNumberMargin();
  void HandleKeyPress(QKeyEvent *event);
  void ShowFind(bool replace);
  void CloseFind();
  void FindNext(bool down);
  void FindIncremental();
  void UpdateFindCount();
  void ReplaceCurrent();
  void ReplaceAll();
  int SearchFlags() const;
  void ShowContextMenu(const QPoint &position);
  void AddSelectionToChat();
  void UpdateAddToChatButton();
  void PositionAddToChatButton();
  void UpdateVisualDebugButton();
  void RequestVisualDebugAt(int position);
  void TrackRangeEdit(int type, int position, int length, const QByteArray &text);
  void ClipboardAction(int action);

  CodeEditorService *m_Services = NULL;
  ScintillaEdit *m_Editor = NULL;
  FindReplace *m_FindReplace = NULL;
  QToolButton *m_DiagnosticSummary = NULL;
  // Floating pill shown next to a selection, so adding shader code to the chat does
  // not require finding a context-menu entry.
  QToolButton *m_AddToChatButton = NULL;
  QToolButton *m_VisualDebugButton = NULL;
  bool m_VisualDebugActive = false;
  bool m_SelectingWithMouse = false;
  int m_FirstDiagnosticLine = 1;
  QString m_FileName;
  QString m_IncludeBaseDirectory;
  QString m_Language = QStringLiteral("hlsl");    // i18n-ignore: LSP language identifier.
  bool m_ForcedModified = false;
  bool m_SettingText = false;
  bool m_NavigationEnabled = true;
  QSet<int> m_LineAnchors;
  struct RangeAnchor
  {
    int start = 0;
    int end = 0;
    bool valid = false;
  };
  struct RangeHistory
  {
    QMap<int, RangeAnchor> before;
    QMap<int, RangeAnchor> after;
  };
  QMap<int, RangeAnchor> m_RangeAnchors;
  QMap<int, int> m_LineAnchorRanges;
  QVector<RangeHistory> m_RangeHistory;
  int m_RangeHistoryCursor = 0;
  int m_NextRangeAnchor = 1;
  QMap<int, RangeAnchor> m_ReplacementRanges;
  int m_ReplacementPosition = -1;
  QMap<int, RangeAnchor> m_CutRanges;
  QByteArray m_CutText;
  int m_ClipboardAction = 0;

  EventCallback m_TextChangedCallback;
  EventCallback m_SaveCallback;
  SelectionCallback m_AddToChatCallback;
  DefinitionCallback m_DefinitionCallback;
  VisualDebugCallback m_VisualDebugCallback;
  EventCallback m_StopVisualDebugCallback;
};
