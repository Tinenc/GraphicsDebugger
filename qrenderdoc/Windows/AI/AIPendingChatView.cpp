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

#include "AIPendingChatView.h"

#include <QAbstractButton>
#include <QCoreApplication>
#include <QEvent>
#include <QFontMetrics>
#include <QHBoxLayout>
#include <QKeyEvent>
#include <QPainter>
#include <QPainterPath>
#include <QPalette>
#include <QPlainTextEdit>
#include <QScrollBar>
#include <QStyle>
#include <QStyleOptionFocusRect>
#include <QVBoxLayout>
#include <utility>

namespace
{
const int CardHeight = 54;
const int EditingCardHeight = 92;
const int CardSpacing = 6;
const int MaximumVisibleCards = 3;

QColor mixedColor(const QColor &background, const QColor &foreground, int foregroundPercent)
{
  const int backgroundPercent = 100 - foregroundPercent;
  return QColor((background.red() * backgroundPercent + foreground.red() * foregroundPercent) /
                    100,
                (background.green() * backgroundPercent + foreground.green() * foregroundPercent) /
                    100,
                (background.blue() * backgroundPercent + foreground.blue() * foregroundPercent) /
                    100,
                background.alpha());
}

class PendingTextLabel : public QWidget
{
public:
  explicit PendingTextLabel(const QString &text, QWidget *parent = 0) : QWidget(parent), m_Text(text)
  {
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Preferred);
    setMinimumWidth(32);
    setToolTip(text);
    setAccessibleName(text);
  }

  QSize sizeHint() const override
  {
    return QSize(fontMetrics().width(m_Text) + 4, fontMetrics().height() + 2);
  }

protected:
  void paintEvent(QPaintEvent *) override
  {
    QPainter painter(this);
    painter.setPen(palette().color(QPalette::WindowText));
    const QString visibleText = fontMetrics().elidedText(m_Text, Qt::ElideRight, width());
    painter.drawText(rect(), Qt::AlignLeft | Qt::AlignVCenter | Qt::TextSingleLine, visibleText);
  }

private:
  QString m_Text;
};

class PendingActionButton : public QAbstractButton
{
public:
  enum Action
  {
    AdjustDirection,
    Edit,
    Delete,
    Save,
    Cancel,
  };

  PendingActionButton(Action action, const QString &toolTip, const QString &visibleText = QString(),
                      QWidget *parent = 0)
      : QAbstractButton(parent), m_Action(action), m_VisibleText(visibleText)
  {
    setFixedHeight(28);
    if(m_VisibleText.isEmpty())
      setFixedWidth(28);
    else
      setMinimumWidth(fontMetrics().width(m_VisibleText) + 18);
    setCursor(Qt::PointingHandCursor);
    setFocusPolicy(Qt::StrongFocus);
    setToolTip(toolTip);
    setAccessibleName(m_VisibleText.isEmpty() ? toolTip : m_VisibleText);
  }

protected:
  void paintEvent(QPaintEvent *) override
  {
    QPainter painter(this);
    painter.setRenderHint(QPainter::Antialiasing, true);

    if(underMouse() || isDown())
    {
      QColor hover = palette().color(QPalette::Highlight);
      hover.setAlpha(isDown() ? 64 : 38);
      painter.setPen(Qt::NoPen);
      painter.setBrush(hover);
      painter.drawRoundedRect(rect().adjusted(1, 1, -1, -1), 6, 6);
    }

    QColor iconColor = palette().color(QPalette::WindowText);
    iconColor.setAlpha(isEnabled() ? 190 : 80);
    QPen pen(iconColor, 1.5, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin);
    painter.setPen(pen);
    painter.setBrush(Qt::NoBrush);

    if(!m_VisibleText.isEmpty())
    {
      painter.drawText(rect().adjusted(7, 0, -7, 0), Qt::AlignCenter | Qt::TextSingleLine,
                       m_VisibleText);
    }
    else
    {
      painter.save();
      const qreal dx = (width() - 24.0) / 2.0;
      const qreal dy = (height() - 24.0) / 2.0;
      painter.translate(dx, dy);

      if(m_Action == Edit)
      {
        QPainterPath pencil;
        pencil.moveTo(7.0, 17.0);
        pencil.lineTo(8.0, 13.0);
        pencil.lineTo(16.0, 5.0);
        pencil.lineTo(19.0, 8.0);
        pencil.lineTo(11.0, 16.0);
        pencil.closeSubpath();
        painter.drawPath(pencil);
        painter.drawLine(QPointF(8.4, 13.0), QPointF(11.0, 15.6));
        painter.drawLine(QPointF(6.5, 18.5), QPointF(17.5, 18.5));
      }
      else if(m_Action == Delete)
      {
        painter.drawRoundedRect(QRectF(8.0, 8.5, 8.0, 10.0), 1.0, 1.0);
        painter.drawLine(QPointF(6.5, 7.0), QPointF(17.5, 7.0));
        painter.drawLine(QPointF(10.0, 5.0), QPointF(14.0, 5.0));
        painter.drawLine(QPointF(10.5, 11.0), QPointF(10.5, 16.0));
        painter.drawLine(QPointF(13.5, 11.0), QPointF(13.5, 16.0));
      }
      else if(m_Action == Save)
      {
        painter.drawLine(QPointF(6.0, 12.5), QPointF(10.5, 17.0));
        painter.drawLine(QPointF(10.5, 17.0), QPointF(18.5, 7.5));
      }
      else if(m_Action == Cancel)
      {
        painter.drawLine(QPointF(7.0, 7.0), QPointF(17.0, 17.0));
        painter.drawLine(QPointF(17.0, 7.0), QPointF(7.0, 17.0));
      }
      painter.restore();
    }

    if(hasFocus())
    {
      QStyleOptionFocusRect focus;
      focus.initFrom(this);
      focus.rect = rect().adjusted(2, 2, -2, -2);
      focus.backgroundColor = palette().color(QPalette::Window);
      style()->drawPrimitive(QStyle::PE_FrameFocusRect, &focus, &painter, this);
    }
  }

private:
  Action m_Action;
  QString m_VisibleText;
};

class PendingPromptEdit : public QPlainTextEdit
{
public:
  explicit PendingPromptEdit(QWidget *parent = 0) : QPlainTextEdit(parent) {}

  std::function<void()> saveHandler;
  std::function<void()> cancelHandler;

protected:
  void keyPressEvent(QKeyEvent *event) override
  {
    if(event->key() == Qt::Key_Escape && cancelHandler)
    {
      cancelHandler();
      event->accept();
      return;
    }
    if((event->key() == Qt::Key_Return || event->key() == Qt::Key_Enter) &&
       (event->modifiers() & (Qt::ControlModifier | Qt::MetaModifier)) && saveHandler)
    {
      saveHandler();
      event->accept();
      return;
    }
    QPlainTextEdit::keyPressEvent(event);
  }
};

class PendingChatCard : public QWidget
{
public:
  typedef std::function<void(const QString &, const QString &)> TextHandler;

  PendingChatCard(const AIPendingChatItem &item,
                  const AIPendingChatView::ItemHandler &adjustHandler,
                  const AIPendingChatView::ItemHandler &beginEditHandler,
                  const TextHandler &draftHandler, const TextHandler &saveHandler,
                  const AIPendingChatView::ItemHandler &cancelHandler,
                  const AIPendingChatView::ItemHandler &deleteHandler, bool editing,
                  const QString &editingDraft, QWidget *parent = 0)
      : QWidget(parent)
  {
    setAttribute(Qt::WA_Hover, true);
    setMouseTracking(true);
    setFixedHeight(editing ? EditingCardHeight : CardHeight);
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    setToolTip(item.text);

    QHBoxLayout *layout = new QHBoxLayout(this);
    layout->setContentsMargins(editing ? 8 : 13, 7, 7, 7);
    layout->setSpacing(3);

    const QString itemId = item.id;
    if(editing)
    {
      m_Editor = new PendingPromptEdit(this);
      m_Editor->setPlainText(editingDraft);
      m_Editor->setFrameStyle(QFrame::NoFrame);
      m_Editor->setPlaceholderText(
          QCoreApplication::translate("AIPendingChatView", "Edit queued message"));
      m_Editor->setToolTip(
          QCoreApplication::translate("AIPendingChatView", "Ctrl+Enter saves; Esc cancels"));
      m_Editor->setAccessibleName(
          QCoreApplication::translate("AIPendingChatView", "Queued message editor"));
      layout->addWidget(m_Editor, 1);

      PendingActionButton *save =
          new PendingActionButton(
              PendingActionButton::Save,
              QCoreApplication::translate("AIPendingChatView", "Save changes"), QString(),
              this);
      PendingActionButton *cancel =
          new PendingActionButton(
              PendingActionButton::Cancel,
              QCoreApplication::translate("AIPendingChatView", "Cancel editing"), QString(),
              this);
      PendingActionButton *remove =
          new PendingActionButton(
              PendingActionButton::Delete,
              QCoreApplication::translate("AIPendingChatView", "Remove from queue"), QString(),
              this);
      save->setEnabled(!editingDraft.trimmed().isEmpty());
      layout->addWidget(save);
      layout->addWidget(cancel);
      layout->addWidget(remove);

      const std::function<void()> saveEdit = [this, saveHandler, itemId]() {
        const QString text = m_Editor->toPlainText();
        if(!text.trimmed().isEmpty() && saveHandler)
          saveHandler(itemId, text);
      };
      m_Editor->saveHandler = saveEdit;
      m_Editor->cancelHandler = [cancelHandler, itemId]() {
        if(cancelHandler)
          cancelHandler(itemId);
      };
      QObject::connect(m_Editor, &QPlainTextEdit::textChanged, this,
                       [this, draftHandler, save, itemId]() {
                         const QString text = m_Editor->toPlainText();
                         save->setEnabled(!text.trimmed().isEmpty());
                         if(draftHandler)
                           draftHandler(itemId, text);
                       });
      QObject::connect(save, &QAbstractButton::clicked, this, saveEdit);
      QObject::connect(cancel, &QAbstractButton::clicked, this, [cancelHandler, itemId]() {
        if(cancelHandler)
          cancelHandler(itemId);
      });
      QObject::connect(remove, &QAbstractButton::clicked, this, [deleteHandler, itemId]() {
        if(deleteHandler)
          deleteHandler(itemId);
      });
    }
    else
    {
      PendingTextLabel *label = new PendingTextLabel(item.text, this);
      layout->addWidget(label, 1);

      PendingActionButton *adjust = new PendingActionButton(
          PendingActionButton::AdjustDirection,
          item.canAdjust
              ? QCoreApplication::translate("AIPendingChatView", "Adjust direction")
              : QCoreApplication::translate("AIPendingChatView",
                                            "No active request in this conversation"),
          QCoreApplication::translate("AIPendingChatView", "Adjust direction"), this);
      adjust->setEnabled(item.canAdjust);
      PendingActionButton *edit = new PendingActionButton(
          PendingActionButton::Edit,
          QCoreApplication::translate("AIPendingChatView", "Edit message"), QString(), this);
      PendingActionButton *remove = new PendingActionButton(
        PendingActionButton::Delete,
        QCoreApplication::translate("AIPendingChatView", "Remove from queue"), QString(), this);
      layout->addWidget(adjust);
      layout->addWidget(edit);
      layout->addWidget(remove);

      QObject::connect(adjust, &QAbstractButton::clicked, this, [adjustHandler, itemId]() {
        if(adjustHandler)
          adjustHandler(itemId);
      });
      QObject::connect(edit, &QAbstractButton::clicked, this, [beginEditHandler, itemId]() {
        if(beginEditHandler)
          beginEditHandler(itemId);
      });
      QObject::connect(remove, &QAbstractButton::clicked, this, [deleteHandler, itemId]() {
        if(deleteHandler)
          deleteHandler(itemId);
      });
    }
  }

  void focusEditor()
  {
    if(!m_Editor)
      return;
    m_Editor->setFocus(Qt::OtherFocusReason);
    QTextCursor cursor = m_Editor->textCursor();
    cursor.movePosition(QTextCursor::End);
    m_Editor->setTextCursor(cursor);
  }

protected:
  void enterEvent(QEvent *event) override
  {
    m_Hovered = true;
    update();
    QWidget::enterEvent(event);
  }

  void leaveEvent(QEvent *event) override
  {
    m_Hovered = false;
    update();
    QWidget::leaveEvent(event);
  }

  void paintEvent(QPaintEvent *) override
  {
    QPainter painter(this);
    painter.setRenderHint(QPainter::Antialiasing, true);

    const QColor window = palette().color(QPalette::Window);
    const QColor text = palette().color(QPalette::WindowText);
    const QColor card = mixedColor(window, text, m_Hovered ? 10 : 7);
    QColor border = mixedColor(window, text, m_Hovered ? 23 : 15);
    border.setAlpha(170);

    painter.setPen(QPen(border, 1.0));
    painter.setBrush(card);
    painter.drawRoundedRect(rect().adjusted(0, 0, -1, -1), 11, 11);
  }

private:
  bool m_Hovered = false;
  PendingPromptEdit *m_Editor = NULL;
};
}

AIPendingChatView::AIPendingChatView(QWidget *parent) : QScrollArea(parent)
{
  setFrameShape(QFrame::NoFrame);
  setWidgetResizable(true);
  setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
  setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
  setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
  setFocusPolicy(Qt::NoFocus);

  m_Content = new QWidget(this);
  m_Content->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Preferred);
  m_CardsLayout = new QVBoxLayout(m_Content);
  m_CardsLayout->setContentsMargins(0, 0, 0, 0);
  m_CardsLayout->setSpacing(CardSpacing);
  m_CardsLayout->setAlignment(Qt::AlignTop);
  setWidget(m_Content);

  viewport()->setAutoFillBackground(false);
  m_Content->setAutoFillBackground(false);
  hide();
}

void AIPendingChatView::setItems(const QVector<AIPendingChatItem> &items)
{
  if(items.size() == m_Items.size())
  {
    bool unchanged = true;
    for(int i = 0; i < items.size(); ++i)
      unchanged &= items[i].id == m_Items[i].id && items[i].text == m_Items[i].text &&
                   items[i].canAdjust == m_Items[i].canAdjust;
    if(unchanged)
      return;
  }
  m_Items = items;
  if(!m_EditingItemId.isEmpty())
  {
    bool editItemStillExists = false;
    for(const AIPendingChatItem &item : m_Items)
      editItemStillExists |= item.id == m_EditingItemId;
    if(!editItemStillExists)
    {
      m_EditingItemId.clear();
      m_EditingDraft.clear();
    }
  }
  rebuildCards();
}

void AIPendingChatView::setAdjustDirectionHandler(ItemHandler handler)
{
  m_AdjustDirectionHandler = std::move(handler);
  rebuildCards();
}

void AIPendingChatView::setEditHandler(ItemHandler handler)
{
  m_EditHandler = std::move(handler);
  rebuildCards();
}

void AIPendingChatView::setDeleteHandler(ItemHandler handler)
{
  m_DeleteHandler = std::move(handler);
  rebuildCards();
}

QSize AIPendingChatView::sizeHint() const
{
  if(m_Items.isEmpty())
    return QSize(420, 0);

  const int visibleCards = qMin(m_Items.size(), MaximumVisibleCards);
  int height = visibleCards * CardHeight + qMax(0, visibleCards - 1) * CardSpacing;
  if(!m_EditingItemId.isEmpty())
    height += EditingCardHeight - CardHeight;
  return QSize(520, height);
}

QSize AIPendingChatView::minimumSizeHint() const
{
  if(m_Items.isEmpty())
    return QSize(180, 0);
  return QSize(180, m_EditingItemId.isEmpty() ? CardHeight : EditingCardHeight);
}

void AIPendingChatView::changeEvent(QEvent *event)
{
  QScrollArea::changeEvent(event);
  if(event && (event->type() == QEvent::PaletteChange || event->type() == QEvent::FontChange ||
               event->type() == QEvent::LanguageChange))
    rebuildCards();
}

void AIPendingChatView::beginEditing(const QString &itemId)
{
  for(const AIPendingChatItem &item : m_Items)
  {
    if(item.id != itemId)
      continue;
    m_EditingItemId = itemId;
    m_EditingDraft = item.text;
    rebuildCards();
    return;
  }
}

void AIPendingChatView::updateEditingDraft(const QString &itemId, const QString &text)
{
  if(itemId == m_EditingItemId)
    m_EditingDraft = text;
}

void AIPendingChatView::saveEditing(const QString &itemId, const QString &text)
{
  if(itemId != m_EditingItemId)
    return;

  const QString edited = text.trimmed();
  if(edited.isEmpty())
    return;

  bool found = false;
  for(AIPendingChatItem &item : m_Items)
  {
    if(item.id == itemId)
    {
      item.text = edited;
      found = true;
      break;
    }
  }
  if(!found)
    return;

  m_EditingItemId.clear();
  m_EditingDraft.clear();
  const ItemHandler editHandler = m_EditHandler;
  rebuildCards();
  if(editHandler)
    editHandler(itemId);
}

void AIPendingChatView::cancelEditing(const QString &itemId)
{
  if(itemId != m_EditingItemId)
    return;
  m_EditingItemId.clear();
  m_EditingDraft.clear();
  rebuildCards();
}

void AIPendingChatView::deleteItem(const QString &itemId)
{
  if(itemId == m_EditingItemId)
  {
    m_EditingItemId.clear();
    m_EditingDraft.clear();
  }
  const ItemHandler deleteHandler = m_DeleteHandler;
  rebuildCards();
  if(deleteHandler)
    deleteHandler(itemId);
}

void AIPendingChatView::rebuildCards()
{
  if(!m_CardsLayout)
    return;

  while(QLayoutItem *layoutItem = m_CardsLayout->takeAt(0))
  {
    if(QWidget *card = layoutItem->widget())
    {
      card->hide();
      card->deleteLater();
    }
    delete layoutItem;
  }

  PendingChatCard *editingCard = NULL;
  for(const AIPendingChatItem &item : m_Items)
  {
    const bool editing = item.id == m_EditingItemId;
    PendingChatCard *card = new PendingChatCard(
        item, m_AdjustDirectionHandler, [this](const QString &itemId) { beginEditing(itemId); },
        [this](const QString &itemId, const QString &text) { updateEditingDraft(itemId, text); },
        [this](const QString &itemId, const QString &text) { saveEditing(itemId, text); },
        [this](const QString &itemId) { cancelEditing(itemId); },
        [this](const QString &itemId) { deleteItem(itemId); }, editing, m_EditingDraft, m_Content);
    m_CardsLayout->addWidget(card);
    if(editing)
      editingCard = card;
  }

  int contentHeight = m_Items.size() * CardHeight + qMax(0, m_Items.size() - 1) * CardSpacing;
  if(editingCard)
    contentHeight += EditingCardHeight - CardHeight;
  m_Content->setMinimumHeight(contentHeight);
  verticalScrollBar()->setSingleStep(CardHeight + CardSpacing);
  verticalScrollBar()->setPageStep(sizeHint().height());
  m_CardsLayout->activate();
  if(editingCard)
  {
    ensureWidgetVisible(editingCard, 0, 0);
    editingCard->focusEditor();
  }
  else
  {
    verticalScrollBar()->setValue(0);
  }
  setVisible(!m_Items.isEmpty());
  updateGeometry();
}
