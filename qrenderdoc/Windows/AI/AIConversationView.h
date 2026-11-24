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

#pragma once

#include <QHash>
#include <QSet>
#include <QJsonArray>
#include <QJsonObject>
#include <QString>
#include <QTextCharFormat>
#include <QTextCursor>
#include <QUrl>
#include <QVector>
#include <QWidget>
#include <functional>

class AITurnRailWidget;
class QTextBrowser;
class QTimer;
class QToolButton;
class AIVisualReply;

struct AIActivityItem
{
  QString id;
  QString kind;
  QString title;
  QString detail;
  qint64 timestampMs = 0;
  qint64 finishedTimestampMs = 0;
  bool completed = false;
  bool error = false;
  // Live Agent messages keep their complete received text in detail. The conversation view owns
  // the transient reveal cursor and inserts one Unicode grapheme at a time; this flag is never
  // restored from persisted history.
  bool liveText = false;
  // Marks detail as owned by an exact Messages delta rather than a cumulative diagnostic
  // snapshot. Runtime lifecycle updates must never replace text after this becomes true.
  bool exactText = false;
  quint64 liveTextRevision = 0;
  int visibleDetailChars = -1;
};

// One explicit piece of user-supplied reference material. Attachment payloads travel separately
// from the user's request so neither RenderDoc routing nor an Agent can mistake instructions in a
// selected file/code fragment for user authority.
struct AIChatAttachment
{
  QString id;
  QString kind;
  QString title;
  QString mediaType;
  QString source;
  QString encoding;
  QString content;
  // Optional bounded, machine-readable provenance (for example capture/session/EID/source
  // selection coordinates). It is metadata attached to untrusted reference material, never user
  // request authority.
  QJsonObject context;
  qint64 originalBytes = 0;
  qint64 modifiedUnixMs = 0;
  bool truncated = false;

  QString annotationPairId() const
  {
    return context.value(QLatin1String("annotation")).toObject().value(QLatin1String("pairId")).toString();
  }
  QString annotationImageRole() const
  {
    return context.value(QLatin1String("annotation"))
        .toObject()
        .value(QLatin1String("imageRole"))
        .toString();
  }
};

// Presentation only: keep both images in persistence and provider input. An orphaned
// original remains visible so a damaged/legacy pair cannot silently hide user data.
inline bool AIHideAnnotationOriginal(const AIChatAttachment &item,
                                     const QVector<AIChatAttachment> &attachments)
{
  if(item.annotationImageRole() == QLatin1String("original") && !item.annotationPairId().isEmpty())
    for(const AIChatAttachment &other : attachments)
      if(other.annotationPairId() == item.annotationPairId() &&
         other.annotationImageRole() == QLatin1String("annotated"))
        return true;
  return false;
}

struct AIChatMessage
{
  QString id;
  QString role;
  QString text;
  QString state;
  QString taskId;
  QString provisionalText;
  qint64 timestampMs = 0;
  qint64 finishedTimestampMs = 0;
  QVector<AIChatAttachment> attachments;
  QVector<AIActivityItem> activity;
  // Files/directories recorded with historical task results (with Host-computed
  // size/change statistics). Rendered as chips below the answer; click opens, right-click offers
  // copy path / show in folder.
  QJsonArray artifacts;
  // Optional hash-addressed native visual answers; old history remains compatible.
  QJsonArray visuals;
  bool activityExpanded = false;
};

// Lightweight, read-only transcript used by AIAssistant. The view renders a deliberately bounded
// Markdown subset and never accepts raw HTML or image markup from model output. All links are
// intercepted so qrenderdoc retains control over external and rdoc:// navigation.
class AIConversationView : public QWidget
{
public:
  explicit AIConversationView(QWidget *parent = 0);

  void setMessages(const QVector<AIChatMessage> &messages);
  void clear();
  void scrollToMessage(const QString &messageId);
  void followSubmittedTurn();

  void setLinkHandler(std::function<void(const QUrl &)> handler);
  void setVisualLinkHandler(std::function<void(const QUrl &, const QJsonObject &)> handler);
  void setActivityToggleHandler(std::function<void(const QString &messageId, bool expanded)> handler);
  void setSelectionAttachmentHandler(std::function<void(const QString &text)> handler);

protected:
  void changeEvent(QEvent *event) override;
  bool eventFilter(QObject *watched, QEvent *event) override;

private:
  struct ViewportAnchor
  {
    QString messageId;
    QString blockText;
    QString context;
    int contextCursorOffset = 0;
    int cursorInBlock = 0;
    int relativeBlock = 0;
    int viewportY = 0;
    bool blockTextIsComplete = false;
    bool valid = false;
  };

  void render(bool preserveScroll = true);
  void activateLink(const QUrl &url);
  void scrollToLatest();
  void detachFromTail();
  void updateTailControl();
  void scheduleUserScrollUpdate();
  void updateFollowTailFromUser();
  void captureViewportAnchor();
  void scheduleViewportAnchorRestore();
  void restoreViewportAnchor();
  void clearViewportAnchor();
  void updateSelectionAction();
  void addSelectedTextToAttachment();
  void flushDeferredRender();
  void updateLiveTextTimer();
  void advanceLiveText();
  void rebuildLiveTextCursors();
  void updateVisualReplies();
  void clearVisualReplies();
  QHash<QString, AIVisualReply *> m_VisualReplies;
  QHash<QString, QTextCursor> m_VisualPositions;
  std::function<void(const QUrl &, const QJsonObject &)> m_VisualLinkHandler;

  struct LiveTextCursor
  {
    QTextCursor cursor;
    QTextCharFormat format;
  };

  QVector<AIChatMessage> m_Messages;
  QHash<QString, QString> m_MessageAnchors;
  AITurnRailWidget *m_TurnRail = NULL;
  QTextBrowser *m_Browser = NULL;
  QToolButton *m_ScrollToLatest = NULL;
  QToolButton *m_SelectionAction = NULL;
  QTimer *m_LiveTextTimer = NULL;
  QHash<QString, QString> m_LiveTextAnchors;
  QHash<QString, LiveTextCursor> m_LiveTextCursors;
  // Elapsed-time labels of running work: updated in place every second through a
  // cursor, never by re-rendering the transcript (a re-render drops a selection).
  struct ElapsedCursor
  {
    QTextCursor cursor;
    QTextCharFormat format;
    int length = 0;
    qint64 startMs = 0;
    // The exact characters this view last wrote into that span. The clock tick
    // replaces the span only when it still holds them, so a document that changed
    // underneath (a re-render, a fragment split between digits and unit) makes the
    // tick rebuild instead of overwriting a span it no longer owns.
    QString text;
  };
  // Store the timestamp and exact rendered text; an HTML anchor marks only the
  // first character, so QTextFragment::length() is not the duration's full length.
  QHash<QString, QPair<qint64, QString>> m_ElapsedAnchors;
  QHash<QString, ElapsedCursor> m_ElapsedCursors;
  QTimer *m_ElapsedTimer = NULL;
  QSet<QString> m_ExpandedArtifactMessages;
  QHash<QString, int> m_ActivityHistoryLimits;
  void rebuildElapsedCursors();
  void advanceElapsedText();
  std::function<void(const QUrl &)> m_LinkHandler;
  std::function<void(const QString &, bool)> m_ActivityToggleHandler;
  std::function<void(const QString &)> m_SelectionAttachmentHandler;
  int m_RenderGeneration = 0;
  quint64 m_UserInteractionGeneration = 0;
  quint64 m_TailResetGeneration = 0;
  int m_PreservedScrollValue = 0;
  ViewportAnchor m_ViewportAnchor;
  bool m_FollowTail = true;
  bool m_AdjustingScroll = false;
  bool m_SelectingText = false;
  bool m_UserScrollUpdatePending = false;
  bool m_ViewportAnchorRestorePending = false;
  bool m_RenderPendingWhileDetached = false;
  bool m_RenderPendingForSelection = false;
};
