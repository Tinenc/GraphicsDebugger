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

#include <QString>
#include <QVector>
#include <QWidget>
#include <functional>

class AIConversationPopup;
class AIConversationPopupDelegate;
class QFocusEvent;
class QKeyEvent;
class QMouseEvent;
class QPaintEvent;
class QResizeEvent;
class QTimer;
class QToolButton;

struct AIConversationListItem
{
  QString id;
  QString title;
  bool pinned = false;
  bool running = false;
  bool unread = false;
};

// Compact, IDE-style conversation tab bar. The view owns no conversation state: callers replace
// the items after each mutation and handle select/pin/delete actions through the callbacks below.
class AIConversationListView : public QWidget
{
public:
  explicit AIConversationListView(QWidget *parent = NULL);

  QSize sizeHint() const override;

  void setItems(const QVector<AIConversationListItem> &items);
  void setActiveId(const QString &conversationId);
  QString activeId() const { return m_ActiveId; }

  void setSelectHandler(std::function<void(const QString &conversationId)> handler);
  void setPinHandler(std::function<void(const QString &conversationId, bool shouldPin)> handler);
  void setDeleteHandler(std::function<void(const QString &conversationId)> handler);

protected:
  void focusInEvent(QFocusEvent *event) override;
  void focusOutEvent(QFocusEvent *event) override;
  void keyPressEvent(QKeyEvent *event) override;
  void leaveEvent(QEvent *event) override;
  void mouseMoveEvent(QMouseEvent *event) override;
  void mousePressEvent(QMouseEvent *event) override;
  void paintEvent(QPaintEvent *event) override;
  void resizeEvent(QResizeEvent *event) override;

private:
  friend class AIConversationPopup;
  friend class AIConversationPopupDelegate;

  const AIConversationListItem *entryAt(int itemIndex) const;
  int activeItemIndex() const;
  int tabAt(const QPoint &position) const;
  QRect pinButtonRect(const QRect &tabRect) const;
  QRect trashButtonRect(const QRect &tabRect) const;
  void activateItem(int itemIndex);
  void rebuildTabs();
  void showAllConversations();
  void updateSpinnerTimer();

  QVector<AIConversationListItem> m_Items;
  QVector<int> m_VisibleItemIndices;
  QVector<QRect> m_TabRects;
  QString m_ActiveId;
  int m_HoveredTab = -1;
  int m_FocusedTab = -1;
  int m_SpinnerAngle = 0;
  QTimer *m_SpinnerTimer = NULL;
  QToolButton *m_AllConversationsButton = NULL;
  AIConversationPopup *m_Popup = NULL;
  std::function<void(const QString &)> m_SelectHandler;
  std::function<void(const QString &, bool)> m_PinHandler;
  std::function<void(const QString &)> m_DeleteHandler;
};
