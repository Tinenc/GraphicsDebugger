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

#include <QScrollArea>
#include <QString>
#include <QVector>
#include <functional>

class QVBoxLayout;

struct AIPendingChatItem
{
  QString id;
  QString text;
  bool canAdjust = false;
};

// Compact queue shown immediately above the AI composer. Each pending prompt is represented by a
// Codex-style rounded card with adjust-direction, inline edit, and remove actions. The view
// deliberately keeps only three cards visible at once and scrolls longer queues so pending work
// cannot consume the transcript area.
class AIPendingChatView : public QScrollArea
{
public:
  typedef std::function<void(const QString &itemId)> ItemHandler;

  explicit AIPendingChatView(QWidget *parent = 0);

  void setItems(const QVector<AIPendingChatItem> &items);
  const QVector<AIPendingChatItem> &items() const { return m_Items; }

  void setAdjustDirectionHandler(ItemHandler handler);
  // The edit handler is invoked after an inline edit has been saved. At that point items() already
  // contains the edited text for itemId, preserving the existing one-argument callback contract.
  void setEditHandler(ItemHandler handler);
  void setDeleteHandler(ItemHandler handler);

  QSize sizeHint() const override;
  QSize minimumSizeHint() const override;

protected:
  void changeEvent(QEvent *event) override;

private:
  void beginEditing(const QString &itemId);
  void updateEditingDraft(const QString &itemId, const QString &text);
  void saveEditing(const QString &itemId, const QString &text);
  void cancelEditing(const QString &itemId);
  void deleteItem(const QString &itemId);
  void rebuildCards();

  QVector<AIPendingChatItem> m_Items;
  QWidget *m_Content = NULL;
  QVBoxLayout *m_CardsLayout = NULL;
  QString m_EditingItemId;
  QString m_EditingDraft;
  ItemHandler m_AdjustDirectionHandler;
  ItemHandler m_EditHandler;
  ItemHandler m_DeleteHandler;
};
