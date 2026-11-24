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

#include "AIConversationListView.h"

#include <QAbstractItemView>
#include <QApplication>
#include <QCoreApplication>
#include <QEvent>
#include <QFocusEvent>
#include <QFontMetrics>
#include <QGuiApplication>
#include <QHideEvent>
#include <QKeyEvent>
#include <QListWidget>
#include <QMouseEvent>
#include <QPaintEvent>
#include <QPainter>
#include <QPainterPath>
#include <QPalette>
#include <QResizeEvent>
#include <QScreen>
#include <QSignalBlocker>
#include <QSizePolicy>
#include <QShowEvent>
#include <QStyle>
#include <QStyleOptionFocusRect>
#include <QStyleOptionViewItem>
#include <QStyledItemDelegate>
#include <QTimer>
#include <QToolButton>
#include <utility>

namespace
{
const int ConversationTabHeight = 32;
const int ConversationTabGap = 2;
const int ConversationTabHorizontalMargin = 2;
const int ConversationTabVerticalMargin = 1;
const int ConversationTabPreferredWidth = 150;
const int ConversationTabMaximumWidth = 180;
const int AllConversationsButtonSize = 28;
const int AllConversationsButtonGap = 3;
const int PopupRowHeight = 36;
const int PopupMaximumRows = 12;
const int PopupMinimumWidth = 280;
const int PopupMaximumWidth = 440;

QColor mixColors(const QColor &base, const QColor &tint, int tintPercent)
{
  const int basePercent = 100 - tintPercent;
  return QColor((base.red() * basePercent + tint.red() * tintPercent) / 100,
                (base.green() * basePercent + tint.green() * tintPercent) / 100,
                (base.blue() * basePercent + tint.blue() * tintPercent) / 100);
}

QPalette conversationListPalette(const QPalette &source)
{
  QPalette result = source;
  const QColor base = source.color(QPalette::Base);
  const QColor text = source.color(QPalette::Text);
  const bool darkTheme = base.lightness() < text.lightness();
  const QColor deepBase = mixColors(base, QColor(0, 0, 0), darkTheme ? 14 : 5);

  // Keep the tab strip visually behind the transcript while preserving the application's
  // foreground and accessibility colours.
  result.setColor(QPalette::Base, deepBase);
  result.setColor(QPalette::AlternateBase, deepBase);
  result.setColor(QPalette::Window, deepBase);
  return result;
}

QColor translucent(QColor color, int alpha)
{
  color.setAlpha(alpha);
  return color;
}

void drawPin(QPainter *painter, const QPoint &center, const QColor &color, bool pinned)
{
  painter->save();
  painter->setRenderHint(QPainter::Antialiasing, true);
  painter->translate(center);

  QPen pen(color, pinned ? 1.9 : 1.6, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin);
  painter->setPen(pen);
  painter->setBrush(pinned ? translucent(color, 55) : Qt::NoBrush);

  QPainterPath body;
  body.moveTo(-4.5, -5.0);
  body.lineTo(4.5, -5.0);
  body.lineTo(2.7, -1.7);
  body.lineTo(2.1, 1.0);
  body.lineTo(-2.1, 1.0);
  body.lineTo(-2.7, -1.7);
  body.closeSubpath();
  painter->drawPath(body);
  painter->drawLine(QPointF(0.0, 1.0), QPointF(0.0, 5.5));
  painter->drawPoint(QPointF(0.0, 6.0));
  painter->restore();
}

void drawTrash(QPainter *painter, const QPoint &center, const QColor &color)
{
  painter->save();
  painter->setRenderHint(QPainter::Antialiasing, true);
  painter->translate(center);
  painter->setPen(QPen(color, 1.65, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
  painter->setBrush(Qt::NoBrush);
  painter->drawLine(QPointF(-5.0, -3.5), QPointF(5.0, -3.5));
  painter->drawLine(QPointF(-1.8, -5.5), QPointF(1.8, -5.5));
  painter->drawRoundedRect(QRectF(-3.8, -1.5, 7.6, 7.2), 1.0, 1.0);
  painter->drawLine(QPointF(-1.3, 0.0), QPointF(-1.3, 4.0));
  painter->drawLine(QPointF(1.3, 0.0), QPointF(1.3, 4.0));
  painter->restore();
}

void drawSpinner(QPainter *painter, const QPoint &center, int angle, const QPalette &palette)
{
  painter->save();
  painter->setRenderHint(QPainter::Antialiasing, true);
  const QRectF spinner(center.x() - 5.0, center.y() - 5.0, 10.0, 10.0);

  const QColor track = mixColors(palette.color(QPalette::Base), palette.color(QPalette::Text), 24);
  painter->setPen(QPen(track, 1.6, Qt::SolidLine, Qt::RoundCap));
  painter->drawArc(spinner, 0, 360 * 16);

  QColor progress = palette.color(QPalette::Highlight);
  if(progress.alpha() == 0)
    progress = QColor(54, 132, 255);
  painter->setPen(QPen(progress, 1.8, Qt::SolidLine, Qt::RoundCap));
  painter->drawArc(spinner, angle * 16, 248 * 16);
  painter->restore();
}

QRect trashActionRect(const QRect &itemRect)
{
  return QRect(itemRect.right() - 25, itemRect.center().y() - 10, 20, 20);
}

QRect pinActionRect(const QRect &itemRect)
{
  const QRect trash = trashActionRect(itemRect);
  return QRect(trash.left() - 23, trash.top(), trash.width(), trash.height());
}

QString conversationTitle(const AIConversationListItem &entry)
{
  const QString title = entry.title.trimmed();
  return title.isEmpty() ? QCoreApplication::translate("AIConversationListView", "New conversation")
                         : title;
}

void paintConversationEntry(QPainter *painter, const QRect &itemRect,
                            const AIConversationListItem &entry, const QPalette &palette,
                            const QFont &font, bool active, bool hovered, bool tab, int spinnerAngle)
{
  painter->save();
  painter->setRenderHint(QPainter::Antialiasing, true);

  const QRect contentRect = itemRect.adjusted(tab ? 0 : 3, 1, tab ? 0 : -3, -1);
  const QColor base = palette.color(QPalette::Base);
  const QColor text = palette.color(QPalette::Text);

  if(active || hovered)
  {
    const int tint = active ? (hovered ? 18 : 15) : 9;
    painter->setPen(active ? QPen(mixColors(base, text, 28), 1.0) : Qt::NoPen);
    painter->setBrush(mixColors(base, text, tint));
    painter->drawRoundedRect(contentRect, tab ? 4.0 : 7.0, tab ? 4.0 : 7.0);
  }

  if(tab && active)
  {
    QColor accent = palette.color(QPalette::Highlight);
    if(accent.alpha() == 0)
      accent = QColor(54, 132, 255);
    painter->setPen(QPen(accent, 2.0, Qt::SolidLine, Qt::RoundCap));
    painter->drawLine(QPointF(contentRect.left() + 5.0, contentRect.bottom() - 0.5),
                      QPointF(contentRect.right() - 5.0, contentRect.bottom() - 0.5));
  }

  const QRect pinRect = pinActionRect(contentRect);
  const QRect trashRect = trashActionRect(contentRect);
  int trailing = contentRect.right() - 9;

  if(hovered)
  {
    trailing = pinRect.left() - 7;
    drawPin(painter, pinRect.center(), translucent(text, entry.pinned ? 245 : 205), entry.pinned);
    drawTrash(painter, trashRect.center(), translucent(text, 205));
  }

  if(entry.running || entry.unread)
  {
    const QPoint stateCenter(hovered ? trailing - 2 : trailing - 3, contentRect.center().y());
    if(entry.running)
      drawSpinner(painter, stateCenter, spinnerAngle, palette);
    else
    {
      painter->setPen(Qt::NoPen);
      painter->setBrush(QColor(54, 132, 255));
      painter->drawEllipse(stateCenter, 3, 3);
    }
    trailing = stateCenter.x() - 11;
  }

  if(entry.pinned && !hovered)
  {
    const QPoint pinCenter(trailing - 2, contentRect.center().y());
    drawPin(painter, pinCenter, translucent(text, 185), true);
    trailing = pinCenter.x() - 11;
  }

  const QRect textRect(contentRect.left() + 10, contentRect.top(),
                       qMax(0, trailing - contentRect.left() - 14), contentRect.height());
  painter->setPen(text);
  painter->setFont(font);
  const QFontMetrics metrics(font);
  painter->drawText(textRect, Qt::AlignVCenter | Qt::AlignLeft,
                    metrics.elidedText(conversationTitle(entry), Qt::ElideRight, textRect.width()));
  painter->restore();
}
}    // namespace

class AIConversationPopup;

class AIConversationPopupDelegate : public QStyledItemDelegate
{
public:
  explicit AIConversationPopupDelegate(AIConversationPopup *popup);

  QSize sizeHint(const QStyleOptionViewItem &, const QModelIndex &) const override
  {
    return QSize(PopupMinimumWidth, PopupRowHeight);
  }

  void paint(QPainter *painter, const QStyleOptionViewItem &option,
             const QModelIndex &index) const override;

private:
  AIConversationPopup *m_Popup = NULL;
};

class AIConversationPopup : public QListWidget
{
public:
  explicit AIConversationPopup(AIConversationListView *owner);

  void syncItems();
  void showForOwner();

protected:
  bool eventFilter(QObject *watched, QEvent *event) override;
  void hideEvent(QHideEvent *event) override;
  void keyPressEvent(QKeyEvent *event) override;
  void leaveEvent(QEvent *event) override;
  void mouseMoveEvent(QMouseEvent *event) override;
  void mousePressEvent(QMouseEvent *event) override;
  void showEvent(QShowEvent *event) override;

private:
  friend class AIConversationPopupDelegate;

  const AIConversationListItem *entryAtRow(int row) const;
  void clearPointerFeedback();
  bool overflowButtonContains(const QPoint &globalPosition) const;

  AIConversationListView *m_Owner = NULL;
  int m_HoveredRow = -1;
  bool m_ApplicationEventFilterInstalled = false;
};

AIConversationPopupDelegate::AIConversationPopupDelegate(AIConversationPopup *popup)
    : QStyledItemDelegate(popup), m_Popup(popup)
{
}

void AIConversationPopupDelegate::paint(QPainter *painter, const QStyleOptionViewItem &option,
                                        const QModelIndex &index) const
{
  const AIConversationListItem *entry = m_Popup->entryAtRow(index.row());
  if(entry == NULL)
    return;

  paintConversationEntry(painter, option.rect, *entry, option.palette, option.font,
                         entry->id == m_Popup->m_Owner->m_ActiveId,
                         index.row() == m_Popup->m_HoveredRow, false,
                         m_Popup->m_Owner->m_SpinnerAngle);
}

AIConversationPopup::AIConversationPopup(AIConversationListView *owner)
    : QListWidget(owner), m_Owner(owner)
{
  setWindowFlags(Qt::Popup | Qt::FramelessWindowHint);
  setPalette(conversationListPalette(owner->palette()));
  setItemDelegate(new AIConversationPopupDelegate(this));
  setMouseTracking(true);
  viewport()->setMouseTracking(true);
  setFrameShape(QFrame::StyledPanel);
  setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
  setVerticalScrollMode(QAbstractItemView::ScrollPerPixel);
  setSelectionMode(QAbstractItemView::SingleSelection);
  setUniformItemSizes(true);
  setSpacing(0);
}

const AIConversationListItem *AIConversationPopup::entryAtRow(int row) const
{
  return m_Owner ? m_Owner->entryAt(row) : NULL;
}

void AIConversationPopup::syncItems()
{
  m_HoveredRow = -1;
  clear();

  if(m_Owner == NULL)
    return;

  const int activeIndex = m_Owner->activeItemIndex();
  for(const AIConversationListItem &entry : m_Owner->m_Items)
  {
    QListWidgetItem *item = new QListWidgetItem(conversationTitle(entry), this);
    item->setData(Qt::UserRole, entry.id);
    item->setToolTip(conversationTitle(entry));
    item->setSizeHint(QSize(PopupMinimumWidth, PopupRowHeight));
  }

  setCurrentRow(activeIndex);
  if(activeIndex >= 0 && item(activeIndex) != NULL)
    scrollToItem(item(activeIndex), QAbstractItemView::PositionAtCenter);
}

void AIConversationPopup::showForOwner()
{
  if(m_Owner == NULL || m_Owner->m_Items.isEmpty())
    return;

  clearPointerFeedback();
  syncItems();

  const QPoint ownerBottomRight = m_Owner->mapToGlobal(m_Owner->rect().bottomRight());
  QScreen *screen = NULL;
  for(QScreen *candidate : QGuiApplication::screens())
  {
    if(candidate && candidate->geometry().contains(ownerBottomRight))
    {
      screen = candidate;
      break;
    }
  }
  if(screen == NULL)
    screen = QGuiApplication::primaryScreen();
  const QRect available = screen ? screen->availableGeometry() : QRect(ownerBottomRight, QSize(1, 1));

  const int popupWidth = qMin(PopupMaximumWidth, qMax(PopupMinimumWidth, m_Owner->width()));
  const int visibleRows = qMin(PopupMaximumRows, qMax(1, count()));
  const int popupHeight = frameWidth() * 2 + visibleRows * PopupRowHeight + 2;
  resize(qMin(popupWidth, available.width()), qMin(popupHeight, available.height()));

  QPoint popupPosition = m_Owner->mapToGlobal(
      QPoint(m_Owner->width() - width(), m_Owner->height() + ConversationTabGap));
  popupPosition.setX(qBound(available.left(), popupPosition.x(), available.right() - width() + 1));
  if(popupPosition.y() + height() > available.bottom() + 1)
    popupPosition.setY(m_Owner->mapToGlobal(QPoint(0, 0)).y() - height() - ConversationTabGap);
  popupPosition.setY(qBound(available.top(), popupPosition.y(), available.bottom() - height() + 1));

  move(popupPosition);
  show();
  raise();
  setFocus(Qt::PopupFocusReason);
}

bool AIConversationPopup::eventFilter(QObject *watched, QEvent *event)
{
  if(isVisible() &&
     (event->type() == QEvent::ApplicationDeactivate ||
      (event->type() == QEvent::WindowDeactivate && watched == this)))
  {
    hide();
    return false;
  }

  if(isVisible() && event->type() == QEvent::MouseMove)
  {
    QMouseEvent *mouseEvent = static_cast<QMouseEvent *>(event);
    if(!rect().contains(mapFromGlobal(mouseEvent->globalPos())))
      clearPointerFeedback();
  }

  if(isVisible() && event->type() == QEvent::MouseButtonPress)
  {
    QMouseEvent *mouseEvent = static_cast<QMouseEvent *>(event);
    const QPoint globalPosition = mouseEvent->globalPos();
    if(!rect().contains(mapFromGlobal(globalPosition)))
    {
      // Qt::Popup normally dismisses itself on an outside press, but a child item view can retain
      // the popup mouse grab on some Qt 5 platforms. Observe application events while visible so
      // an outside press always releases the grab. Consume a press on the overflow button to stop
      // its checkable state from reopening the popup as part of the same click.
      const bool overflowButtonPress =
          mouseEvent->button() == Qt::LeftButton && overflowButtonContains(globalPosition);
      hide();
      if(overflowButtonPress)
      {
        event->accept();
        return true;
      }
    }
  }

  return QListWidget::eventFilter(watched, event);
}

void AIConversationPopup::hideEvent(QHideEvent *event)
{
  if(m_ApplicationEventFilterInstalled && qApp)
  {
    qApp->removeEventFilter(this);
    m_ApplicationEventFilterInstalled = false;
  }

  clearPointerFeedback();
  if(m_Owner && m_Owner->m_AllConversationsButton)
  {
    const QSignalBlocker blocker(m_Owner->m_AllConversationsButton);
    m_Owner->m_AllConversationsButton->setChecked(false);
  }
  QListWidget::hideEvent(event);
}

void AIConversationPopup::showEvent(QShowEvent *event)
{
  QListWidget::showEvent(event);
  if(!m_ApplicationEventFilterInstalled && qApp)
  {
    qApp->installEventFilter(this);
    m_ApplicationEventFilterInstalled = true;
  }
}

void AIConversationPopup::clearPointerFeedback()
{
  m_HoveredRow = -1;
  unsetCursor();
  setToolTip(QString());
  if(viewport())
  {
    viewport()->unsetCursor();
    viewport()->setToolTip(QString());
  }

  if(m_Owner)
  {
    m_Owner->m_HoveredTab = -1;
    m_Owner->unsetCursor();
    m_Owner->setToolTip(QString());
    if(m_Owner->m_AllConversationsButton)
      m_Owner->m_AllConversationsButton->unsetCursor();
    m_Owner->update();
  }
}

bool AIConversationPopup::overflowButtonContains(const QPoint &globalPosition) const
{
  if(m_Owner == NULL || m_Owner->m_AllConversationsButton == NULL ||
     !m_Owner->m_AllConversationsButton->isVisible())
    return false;

  const QToolButton *button = m_Owner->m_AllConversationsButton;
  return QRect(button->mapToGlobal(QPoint(0, 0)), button->size()).contains(globalPosition);
}

void AIConversationPopup::keyPressEvent(QKeyEvent *event)
{
  if(event->key() == Qt::Key_Escape)
  {
    hide();
    event->accept();
    return;
  }

  if((event->key() == Qt::Key_Return || event->key() == Qt::Key_Enter ||
      event->key() == Qt::Key_Space) &&
     currentRow() >= 0)
  {
    const int row = currentRow();
    hide();
    m_Owner->activateItem(row);
    event->accept();
    return;
  }

  QListWidget::keyPressEvent(event);
}

void AIConversationPopup::leaveEvent(QEvent *event)
{
  clearPointerFeedback();
  QListWidget::leaveEvent(event);
}

void AIConversationPopup::mouseMoveEvent(QMouseEvent *event)
{
  const QModelIndex index = indexAt(event->pos());
  const int hovered = index.isValid() ? index.row() : -1;
  if(hovered != m_HoveredRow)
  {
    m_HoveredRow = hovered;
    viewport()->update();
  }

  const AIConversationListItem *entry = index.isValid() ? entryAtRow(index.row()) : NULL;
  if(entry)
  {
    const QRect itemRect = visualRect(index).adjusted(3, 1, -3, -1);
    if(pinActionRect(itemRect).contains(event->pos()))
      setToolTip(entry->pinned
                     ? QCoreApplication::translate("AIConversationListView", "Unpin conversation")
                     : QCoreApplication::translate("AIConversationListView", "Pin conversation"));
    else if(trashActionRect(itemRect).contains(event->pos()))
      setToolTip(QCoreApplication::translate("AIConversationListView", "Delete conversation"));
    else
      setToolTip(QString());
    setCursor(Qt::PointingHandCursor);
    viewport()->setCursor(Qt::PointingHandCursor);
  }
  else
  {
    clearPointerFeedback();
  }

  QListWidget::mouseMoveEvent(event);
}

void AIConversationPopup::mousePressEvent(QMouseEvent *event)
{
  if(event->button() != Qt::LeftButton)
  {
    QListWidget::mousePressEvent(event);
    return;
  }

  const QModelIndex index = indexAt(event->pos());
  const AIConversationListItem *entry = index.isValid() ? entryAtRow(index.row()) : NULL;
  if(entry == NULL)
  {
    hide();
    event->accept();
    return;
  }

  const int row = index.row();
  const QString conversationId = entry->id;
  const bool pinned = entry->pinned;
  const QRect itemRect = visualRect(index).adjusted(3, 1, -3, -1);
  if(m_HoveredRow == row && pinActionRect(itemRect).contains(event->pos()))
  {
    if(m_Owner->m_PinHandler)
      m_Owner->m_PinHandler(conversationId, !pinned);
    event->accept();
    return;
  }
  if(m_HoveredRow == row && trashActionRect(itemRect).contains(event->pos()))
  {
    if(m_Owner->m_DeleteHandler)
      m_Owner->m_DeleteHandler(conversationId);
    event->accept();
    return;
  }

  QListWidget::mousePressEvent(event);
  hide();
  m_Owner->activateItem(row);
  event->accept();
}

AIConversationListView::AIConversationListView(QWidget *parent) : QWidget(parent)
{
  setPalette(conversationListPalette(palette()));
  setAutoFillBackground(true);
  setBackgroundRole(QPalette::Base);
  setFocusPolicy(Qt::StrongFocus);
  setMouseTracking(true);
  setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);

  m_AllConversationsButton = new QToolButton(this);
  m_AllConversationsButton->setAutoRaise(true);
  m_AllConversationsButton->setArrowType(Qt::DownArrow);
  m_AllConversationsButton->setCheckable(true);
  m_AllConversationsButton->setEnabled(false);
  const QString conversations = QCoreApplication::translate("AIAssistant", "Conversations");
  setAccessibleName(conversations);
  m_AllConversationsButton->setToolTip(conversations);
  m_AllConversationsButton->setAccessibleName(conversations);
  QObject::connect(m_AllConversationsButton, &QToolButton::clicked, this, [this](bool checked) {
    if(checked)
      showAllConversations();
    else if(m_Popup)
      m_Popup->hide();
  });

  m_Popup = new AIConversationPopup(this);

  m_SpinnerTimer = new QTimer(this);
  m_SpinnerTimer->setInterval(90);
  QObject::connect(m_SpinnerTimer, &QTimer::timeout, this, [this]() {
    m_SpinnerAngle = (m_SpinnerAngle + 338) % 360;
    update();
    if(m_Popup && m_Popup->isVisible())
      m_Popup->viewport()->update();
  });

  rebuildTabs();
}

QSize AIConversationListView::sizeHint() const
{
  return QSize(420, minimumHeight());
}

void AIConversationListView::setItems(const QVector<AIConversationListItem> &items)
{
  QString focusedId;
  if(m_FocusedTab >= 0 && m_FocusedTab < m_VisibleItemIndices.size())
  {
    const AIConversationListItem *focused = entryAt(m_VisibleItemIndices[m_FocusedTab]);
    if(focused)
      focusedId = focused->id;
  }

  m_Items = items;
  m_HoveredTab = -1;
  m_FocusedTab = -1;
  rebuildTabs();
  if(!focusedId.isEmpty())
  {
    for(int tab = 0; tab < m_VisibleItemIndices.size(); ++tab)
    {
      const AIConversationListItem *entry = entryAt(m_VisibleItemIndices[tab]);
      if(entry && entry->id == focusedId)
      {
        m_FocusedTab = tab;
        break;
      }
    }
  }
  m_AllConversationsButton->setEnabled(!m_Items.isEmpty());
  if(m_Popup && m_Popup->isVisible())
    m_Popup->syncItems();
  updateSpinnerTimer();
  update();
}

void AIConversationListView::setActiveId(const QString &conversationId)
{
  if(m_ActiveId == conversationId)
  {
    update();
    if(m_Popup && m_Popup->isVisible())
      m_Popup->viewport()->update();
    return;
  }

  m_ActiveId = conversationId;
  m_FocusedTab = -1;
  rebuildTabs();
  if(m_Popup && m_Popup->isVisible())
    m_Popup->syncItems();
  update();
}

void AIConversationListView::setSelectHandler(std::function<void(const QString &)> handler)
{
  m_SelectHandler = std::move(handler);
}

void AIConversationListView::setPinHandler(std::function<void(const QString &, bool)> handler)
{
  m_PinHandler = std::move(handler);
}

void AIConversationListView::setDeleteHandler(std::function<void(const QString &)> handler)
{
  m_DeleteHandler = std::move(handler);
}

const AIConversationListItem *AIConversationListView::entryAt(int itemIndex) const
{
  if(itemIndex < 0 || itemIndex >= m_Items.size())
    return NULL;
  return &m_Items[itemIndex];
}

int AIConversationListView::activeItemIndex() const
{
  for(int i = 0; i < m_Items.size(); ++i)
    if(m_Items[i].id == m_ActiveId)
      return i;
  return -1;
}

int AIConversationListView::tabAt(const QPoint &position) const
{
  for(int i = 0; i < m_TabRects.size(); ++i)
    if(m_TabRects[i].contains(position))
      return i;
  return -1;
}

QRect AIConversationListView::trashButtonRect(const QRect &tabRect) const
{
  return trashActionRect(tabRect);
}

QRect AIConversationListView::pinButtonRect(const QRect &tabRect) const
{
  return pinActionRect(tabRect);
}

void AIConversationListView::activateItem(int itemIndex)
{
  const AIConversationListItem *entry = entryAt(itemIndex);
  if(entry == NULL)
    return;
  // A handler can synchronously rebuild the view, so never expose a reference into m_Items.
  const QString conversationId = entry->id;
  setActiveId(conversationId);
  if(m_SelectHandler)
    m_SelectHandler(conversationId);
}

void AIConversationListView::rebuildTabs()
{
  QString focusedId;
  if(m_FocusedTab >= 0 && m_FocusedTab < m_VisibleItemIndices.size())
  {
    const AIConversationListItem *focused = entryAt(m_VisibleItemIndices[m_FocusedTab]);
    if(focused)
      focusedId = focused->id;
  }

  const int tabAreaWidth = qMax(1, width() - ConversationTabHorizontalMargin * 2 -
                                       AllConversationsButtonSize - AllConversationsButtonGap);
  const int columns = qMax(
      1, (tabAreaWidth + ConversationTabGap) / (ConversationTabPreferredWidth + ConversationTabGap));
  const int tabWidth = qMin(ConversationTabMaximumWidth,
                            qMax(1, (tabAreaWidth - (columns - 1) * ConversationTabGap) / columns));
  const int capacity = columns * 2;

  m_VisibleItemIndices.clear();
  m_VisibleItemIndices.reserve(qMin(capacity, m_Items.size()));
  for(int i = 0; i < m_Items.size() && i < capacity; ++i)
    m_VisibleItemIndices.push_back(i);

  const int activeIndex = activeItemIndex();
  if(activeIndex >= 0 && !m_VisibleItemIndices.contains(activeIndex) &&
     !m_VisibleItemIndices.isEmpty())
  {
    // Preserve the caller's status ordering while guaranteeing that the active conversation is
    // never hidden behind the overflow control. The active item is later in that order, so it
    // replaces the last ordinary visible tab.
    m_VisibleItemIndices.back() = activeIndex;
  }

  m_TabRects.clear();
  m_TabRects.reserve(m_VisibleItemIndices.size());
  for(int tab = 0; tab < m_VisibleItemIndices.size(); ++tab)
  {
    const int row = tab / columns;
    const int column = tab % columns;
    m_TabRects.push_back(
        QRect(ConversationTabHorizontalMargin + column * (tabWidth + ConversationTabGap),
              ConversationTabVerticalMargin + row * (ConversationTabHeight + ConversationTabGap),
              tabWidth, ConversationTabHeight));
  }

  const int rows = m_VisibleItemIndices.size() > columns ? 2 : 1;
  const int desiredHeight = ConversationTabVerticalMargin * 2 + rows * ConversationTabHeight +
                            (rows - 1) * ConversationTabGap;
  if(minimumHeight() != desiredHeight || maximumHeight() != desiredHeight)
  {
    setFixedHeight(desiredHeight);
    updateGeometry();
  }

  m_AllConversationsButton->setGeometry(
      qMax(0, width() - ConversationTabHorizontalMargin - AllConversationsButtonSize),
      ConversationTabVerticalMargin + (ConversationTabHeight - AllConversationsButtonSize) / 2,
      AllConversationsButtonSize, AllConversationsButtonSize);

  int newFocusedTab = -1;
  for(int tab = 0; tab < m_VisibleItemIndices.size() && !focusedId.isEmpty(); ++tab)
  {
    const AIConversationListItem *entry = entryAt(m_VisibleItemIndices[tab]);
    if(entry && entry->id == focusedId)
    {
      newFocusedTab = tab;
      break;
    }
  }
  if(newFocusedTab < 0)
    newFocusedTab = m_VisibleItemIndices.indexOf(activeIndex);
  if(newFocusedTab < 0 && !m_VisibleItemIndices.isEmpty())
    newFocusedTab = 0;
  m_FocusedTab = newFocusedTab;
  m_HoveredTab = -1;
  update();
}

void AIConversationListView::showAllConversations()
{
  if(m_Popup == NULL || m_Items.isEmpty())
  {
    const QSignalBlocker blocker(m_AllConversationsButton);
    m_AllConversationsButton->setChecked(false);
    return;
  }

  m_AllConversationsButton->setChecked(true);
  m_Popup->showForOwner();
}

void AIConversationListView::updateSpinnerTimer()
{
  bool hasRunningItem = false;
  for(const AIConversationListItem &entry : m_Items)
    hasRunningItem |= entry.running;

  if(hasRunningItem)
  {
    if(!m_SpinnerTimer->isActive())
      m_SpinnerTimer->start();
  }
  else
  {
    m_SpinnerTimer->stop();
    m_SpinnerAngle = 0;
  }
}

void AIConversationListView::focusInEvent(QFocusEvent *event)
{
  if(m_FocusedTab < 0)
  {
    m_FocusedTab = m_VisibleItemIndices.indexOf(activeItemIndex());
    if(m_FocusedTab < 0 && !m_VisibleItemIndices.isEmpty())
      m_FocusedTab = 0;
  }
  update();
  QWidget::focusInEvent(event);
}

void AIConversationListView::focusOutEvent(QFocusEvent *event)
{
  update();
  QWidget::focusOutEvent(event);
}

void AIConversationListView::keyPressEvent(QKeyEvent *event)
{
  if(event->key() == Qt::Key_Down && event->modifiers().testFlag(Qt::AltModifier))
  {
    showAllConversations();
    event->accept();
    return;
  }

  if(m_VisibleItemIndices.isEmpty())
  {
    QWidget::keyPressEvent(event);
    return;
  }

  int targetTab = m_FocusedTab;
  if(targetTab < 0 || targetTab >= m_VisibleItemIndices.size())
    targetTab = 0;

  int firstRowCount = 0;
  if(!m_TabRects.isEmpty())
  {
    const int firstRowTop = m_TabRects.front().top();
    while(firstRowCount < m_TabRects.size() && m_TabRects[firstRowCount].top() == firstRowTop)
      ++firstRowCount;
  }

  switch(event->key())
  {
    case Qt::Key_Left: targetTab = qMax(0, targetTab - 1); break;
    case Qt::Key_Right: targetTab = qMin(m_VisibleItemIndices.size() - 1, targetTab + 1); break;
    case Qt::Key_Up:
      if(firstRowCount > 0)
        targetTab = qMax(0, targetTab - firstRowCount);
      break;
    case Qt::Key_Down:
      if(firstRowCount > 0)
        targetTab = qMin(m_VisibleItemIndices.size() - 1, targetTab + firstRowCount);
      break;
    case Qt::Key_Home: targetTab = 0; break;
    case Qt::Key_End: targetTab = m_VisibleItemIndices.size() - 1; break;
    case Qt::Key_Return:
    case Qt::Key_Enter:
    case Qt::Key_Space:
      if(m_FocusedTab >= 0 && m_FocusedTab < m_VisibleItemIndices.size())
        activateItem(m_VisibleItemIndices[m_FocusedTab]);
      event->accept();
      return;
    default: QWidget::keyPressEvent(event); return;
  }

  m_FocusedTab = targetTab;
  update();
  event->accept();
}

void AIConversationListView::leaveEvent(QEvent *event)
{
  if(m_HoveredTab != -1)
  {
    m_HoveredTab = -1;
    update();
  }
  unsetCursor();
  setToolTip(QString());
  QWidget::leaveEvent(event);
}

void AIConversationListView::mouseMoveEvent(QMouseEvent *event)
{
  const int hovered = tabAt(event->pos());
  if(hovered != m_HoveredTab)
  {
    m_HoveredTab = hovered;
    update();
  }

  const int itemIndex = hovered >= 0 ? m_VisibleItemIndices[hovered] : -1;
  const AIConversationListItem *entry = entryAt(itemIndex);
  if(entry)
  {
    const QRect tabRect = m_TabRects[hovered];
    if(pinButtonRect(tabRect).contains(event->pos()))
      setToolTip(entry->pinned
                     ? QCoreApplication::translate("AIConversationListView", "Unpin conversation")
                     : QCoreApplication::translate("AIConversationListView", "Pin conversation"));
    else if(trashButtonRect(tabRect).contains(event->pos()))
      setToolTip(QCoreApplication::translate("AIConversationListView", "Delete conversation"));
    else
      setToolTip(conversationTitle(*entry));
    setCursor(Qt::PointingHandCursor);
  }
  else
  {
    unsetCursor();
    setToolTip(QString());
  }

  QWidget::mouseMoveEvent(event);
}

void AIConversationListView::mousePressEvent(QMouseEvent *event)
{
  if(event->button() != Qt::LeftButton)
  {
    QWidget::mousePressEvent(event);
    return;
  }

  const int tab = tabAt(event->pos());
  if(tab < 0 || tab >= m_VisibleItemIndices.size())
  {
    QWidget::mousePressEvent(event);
    return;
  }

  const int itemIndex = m_VisibleItemIndices[tab];
  const AIConversationListItem *entry = entryAt(itemIndex);
  if(entry == NULL)
    return;

  setFocus(Qt::MouseFocusReason);
  m_FocusedTab = tab;
  const QString conversationId = entry->id;
  const bool pinned = entry->pinned;
  const QRect tabRect = m_TabRects[tab];
  if(m_HoveredTab == tab && pinButtonRect(tabRect).contains(event->pos()))
  {
    if(m_PinHandler)
      m_PinHandler(conversationId, !pinned);
    event->accept();
    return;
  }
  if(m_HoveredTab == tab && trashButtonRect(tabRect).contains(event->pos()))
  {
    if(m_DeleteHandler)
      m_DeleteHandler(conversationId);
    event->accept();
    return;
  }

  activateItem(itemIndex);
  event->accept();
}

void AIConversationListView::paintEvent(QPaintEvent *event)
{
  QPainter painter(this);
  painter.setClipRegion(event->region());

  for(int tab = 0; tab < m_VisibleItemIndices.size() && tab < m_TabRects.size(); ++tab)
  {
    const AIConversationListItem *entry = entryAt(m_VisibleItemIndices[tab]);
    if(entry == NULL)
      continue;

    paintConversationEntry(&painter, m_TabRects[tab], *entry, palette(), font(),
                           entry->id == m_ActiveId, tab == m_HoveredTab, true, m_SpinnerAngle);

    if(hasFocus() && tab == m_FocusedTab)
    {
      QStyleOptionFocusRect focus;
      focus.initFrom(this);
      focus.rect = m_TabRects[tab].adjusted(3, 3, -3, -3);
      focus.backgroundColor = palette().color(QPalette::Base);
      style()->drawPrimitive(QStyle::PE_FrameFocusRect, &focus, &painter, this);
    }
  }
}

void AIConversationListView::resizeEvent(QResizeEvent *event)
{
  QWidget::resizeEvent(event);
  rebuildTabs();
}
