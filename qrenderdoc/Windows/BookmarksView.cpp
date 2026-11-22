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

#include "BookmarksView.h"

#include <functional>
#include <QApplication>
#include <QCursor>
#include <QDropEvent>
#include <QFont>
#include <QInputDialog>
#include <QItemSelectionModel>
#include <QKeyEvent>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QMenu>
#include <QMouseEvent>
#include <QPainter>
#include <QSet>
#include <QScrollBar>
#include <QStackedLayout>
#include <QStyledItemDelegate>

#include "Code/CaptureContext.h"
#include "Code/QRDUtils.h"
#include "Code/Resources.h"
#include "Windows/EventBrowser.h"

namespace
{
enum
{
  BookmarkEIDRole = Qt::UserRole + 1,
};

class BookmarkItemDelegate : public QStyledItemDelegate
{
public:
  explicit BookmarkItemDelegate(QObject *parent = NULL) : QStyledItemDelegate(parent) {}

  void paint(QPainter *painter, const QStyleOptionViewItem &option,
             const QModelIndex &index) const override
  {
    if(!(option.state & QStyle::State_MouseOver))
    {
      QStyledItemDelegate::paint(painter, option, index);
      return;
    }

    const QRect iconRect = deleteIconRect(option.rect, option.widget);
    QStyleOptionViewItem fullOption(option);
    initStyleOption(&fullOption, index);

    QStyleOptionViewItem backgroundOption(fullOption);
    backgroundOption.text.clear();
    backgroundOption.icon = QIcon();

    QStyle *style = option.widget ? option.widget->style() : QApplication::style();
    style->drawControl(QStyle::CE_ItemViewItem, &backgroundOption, painter, option.widget);

    QStyleOptionViewItem contentOption(fullOption);
    contentOption.rect.setRight(qMax(contentOption.rect.left(), iconRect.left() - 4));
    contentOption.state &= ~QStyle::State_HasFocus;
    style->drawControl(QStyle::CE_ItemViewItem, &contentOption, painter, option.widget);

    Icons::del().paint(painter, iconRect);
  }

  static QRect deleteIconRect(const QRect &itemRect, const QWidget *widget)
  {
    const int extent = QApplication::style()->pixelMetric(QStyle::PM_SmallIconSize, NULL, widget);
    const int margin = 5;
    return QRect(itemRect.right() - extent - margin + 1, itemRect.center().y() - extent / 2,
                 extent, extent);
  }
};
}

class BookmarkListWidget : public QListWidget
{
public:
  explicit BookmarkListWidget(QWidget *parent = NULL) : QListWidget(parent)
  {
    setAlternatingRowColors(false);
    setDefaultDropAction(Qt::MoveAction);
    setDragDropMode(QAbstractItemView::InternalMove);
    setDragDropOverwriteMode(false);
    setDropIndicatorShown(true);
    setMouseTracking(true);
    setSelectionMode(QAbstractItemView::ExtendedSelection);
    setSelectionBehavior(QAbstractItemView::SelectRows);
  }

  std::function<void()> bookmarkOrderChanged;
  std::function<void(const QList<uint32_t> &)> bookmarksDraggedOutside;
  std::function<void()> deleteSelection;
  std::function<void()> renameSelection;
  std::function<void(uint32_t)> deleteBookmark;
  std::function<void()> dragFinished;
  QWidget *removalBoundary = NULL;

  bool dragInProgress() const { return m_DragInProgress; }

protected:
  void mousePressEvent(QMouseEvent *event) override
  {
    m_DeletePressActive = false;

    QListWidgetItem *item = NULL;
    if(event->button() == Qt::LeftButton && deleteIconHit(event->pos(), item))
    {
      m_DeletePressActive = true;
      m_DeletePressEID = item->data(BookmarkEIDRole).toUInt();
      event->accept();
      return;
    }

    QListWidget::mousePressEvent(event);
  }

  void mouseReleaseEvent(QMouseEvent *event) override
  {
    if(m_DeletePressActive)
    {
      QListWidgetItem *item = NULL;
      const bool deleteConfirmed =
          event->button() == Qt::LeftButton && deleteIconHit(event->pos(), item) &&
          item->data(BookmarkEIDRole).toUInt() == m_DeletePressEID;
      const uint32_t EID = m_DeletePressEID;
      m_DeletePressActive = false;

      if(deleteConfirmed && deleteBookmark)
        deleteBookmark(EID);

      event->accept();
      return;
    }

    QListWidget::mouseReleaseEvent(event);
  }

  void mouseDoubleClickEvent(QMouseEvent *event) override
  {
    QListWidgetItem *item = NULL;
    if(event->button() == Qt::LeftButton && deleteIconHit(event->pos(), item))
    {
      m_DeletePressActive = false;
      event->accept();
      return;
    }

    QListWidget::mouseDoubleClickEvent(event);
  }

  void mouseMoveEvent(QMouseEvent *event) override
  {
    if(m_DeletePressActive)
    {
      event->accept();
      return;
    }

    QListWidget::mouseMoveEvent(event);
  }

  void keyPressEvent(QKeyEvent *event) override
  {
    if(event->key() == Qt::Key_Delete && event->modifiers() == Qt::NoModifier)
    {
      if(deleteSelection)
        deleteSelection();
      event->accept();
      return;
    }

    if(event->key() == Qt::Key_F2 && event->modifiers() == Qt::NoModifier)
    {
      if(renameSelection)
        renameSelection();
      event->accept();
      return;
    }

    QListWidget::keyPressEvent(event);
  }

  void startDrag(Qt::DropActions supportedActions) override
  {
    m_DragInProgress = true;
    m_DroppedInternally = false;
    m_DraggedEIDs.clear();
    for(QListWidgetItem *item : selectedItems())
      m_DraggedEIDs.push_back(item->data(BookmarkEIDRole).toUInt());

    QListWidget::startDrag(supportedActions);
    m_DragInProgress = false;

    if(m_DroppedInternally && bookmarkOrderChanged)
      bookmarkOrderChanged();

    QWidget *boundary = removalBoundary ? removalBoundary : this;
    const bool cursorOutside =
        !boundary->rect().contains(boundary->mapFromGlobal(QCursor::pos()));
    if(!m_DroppedInternally && cursorOutside && QApplication::mouseButtons() == Qt::NoButton &&
       !m_DraggedEIDs.isEmpty() && bookmarksDraggedOutside)
      bookmarksDraggedOutside(m_DraggedEIDs);

    m_DraggedEIDs.clear();
    if(dragFinished)
      dragFinished();
  }

  void dropEvent(QDropEvent *event) override
  {
    const bool internalDrop = event->source() == this;
    QListWidget::dropEvent(event);

    if(internalDrop && event->isAccepted())
      m_DroppedInternally = true;
  }

private:
  bool deleteIconHit(const QPoint &pos, QListWidgetItem *&item) const
  {
    item = itemAt(pos);
    return item &&
           BookmarkItemDelegate::deleteIconRect(visualItemRect(item), this).contains(pos);
  }

  bool m_DroppedInternally = false;
  bool m_DragInProgress = false;
  bool m_DeletePressActive = false;
  uint32_t m_DeletePressEID = 0;
  QList<uint32_t> m_DraggedEIDs;
};

BookmarksView::BookmarksView(CaptureContext &ctx, QWidget *parent) : QFrame(parent), m_Ctx(ctx)
{
  setWindowTitle(tr("Bookmarks"));
  setWindowIcon(Icons::bookmark_blue());

  QStackedLayout *layout = new QStackedLayout(this);
  layout->setContentsMargins(0, 0, 0, 0);

  m_List = new BookmarkListWidget(this);
  m_List->removalBoundary = this;
  m_List->setContextMenuPolicy(Qt::CustomContextMenu);
  m_List->setToolTip(
      tr("Double-click a bookmark to jump to its EID. Drag it outside this window to remove it."));

  BookmarkItemDelegate *delegate = new BookmarkItemDelegate(m_List);
  m_List->setItemDelegate(delegate);
  m_List->deleteBookmark = [this](uint32_t EID) { deleteBookmark(EID); };

  m_EmptyLabel = new QLabel(this);
  m_EmptyLabel->setAlignment(Qt::AlignCenter);
  m_EmptyLabel->setMargin(16);
  m_EmptyLabel->setWordWrap(true);

  layout->addWidget(m_List);
  layout->addWidget(m_EmptyLabel);

  QObject::connect(m_List, &QListWidget::customContextMenuRequested, this,
                   &BookmarksView::showContextMenu);
  QObject::connect(m_List, &QListWidget::itemDoubleClicked,
                   [this](QListWidgetItem *item) {
                     if(item)
                       jumpToBookmark(item->data(BookmarkEIDRole).toUInt());
                   });

  m_List->deleteSelection = [this]() { deleteSelectedBookmarks(); };
  m_List->renameSelection = [this]() {
    if(m_List->selectedItems().count() == 1)
      renameBookmark(m_List->selectedItems()[0]->data(BookmarkEIDRole).toUInt());
  };
  m_List->bookmarkOrderChanged = [this]() { saveBookmarkOrder(); };
  m_List->bookmarksDraggedOutside =
      [this](const QList<uint32_t> &EIDs) { deleteBookmarks(EIDs); };
  m_List->dragFinished = [this]() {
    if(m_RefreshAfterDrag)
      BookmarksChanged();
  };

  m_Ctx.AddCaptureViewer(this);
  BookmarksChanged();
}

BookmarksView::~BookmarksView()
{
  m_Ctx.BuiltinWindowClosed(this);
  m_Ctx.RemoveCaptureViewer(this);
}

void BookmarksView::OnCaptureLoaded()
{
  BookmarksChanged();
}

void BookmarksView::OnCaptureClosed()
{
  BookmarksChanged();
}

void BookmarksView::OnSelectedEventChanged(uint32_t eventId)
{
  updateCurrentBookmark(eventId);
}

void BookmarksView::OnEventChanged(uint32_t eventId)
{
  Q_UNUSED(eventId);
  updateCurrentBookmark(m_Ctx.CurSelectedEvent());
}

void BookmarksView::BookmarksChanged()
{
  if(m_List->dragInProgress())
  {
    m_RefreshAfterDrag = true;
    return;
  }

  if(m_Refreshing)
    return;

  m_Refreshing = true;
  m_RefreshAfterDrag = false;
  const int scrollValue = m_List->verticalScrollBar()->value();

  QSet<uint32_t> selectedEIDs;
  for(QListWidgetItem *item : m_List->selectedItems())
    selectedEIDs.insert(item->data(BookmarkEIDRole).toUInt());

  const bool hadCurrentItem = m_List->currentItem() != NULL;
  uint32_t currentEID = 0;
  if(hadCurrentItem)
    currentEID = m_List->currentItem()->data(BookmarkEIDRole).toUInt();

  m_List->clear();

  if(m_Ctx.IsCaptureLoaded())
  {
    for(const EventBookmark &bookmark : m_Ctx.GetBookmarks())
    {
      const QString displayText = bookmark.text.empty()
                                      ? tr("EID %1").arg(bookmark.eventId)
                                      : tr("%1 (EID %2)").arg((QString)bookmark.text).arg(
                                            bookmark.eventId);
      QListWidgetItem *item = new QListWidgetItem(Icons::asterisk_orange(), displayText, m_List);
      item->setData(BookmarkEIDRole, bookmark.eventId);
      item->setToolTip(
          tr("Double-click to jump to EID %1. Drag outside this window to remove.")
              .arg(bookmark.eventId));
      if(selectedEIDs.contains(bookmark.eventId))
        item->setSelected(true);
      if(hadCurrentItem && currentEID == bookmark.eventId)
        m_List->setCurrentItem(item, QItemSelectionModel::NoUpdate);
    }
  }

  const bool empty = m_List->count() == 0;
  if(!m_Ctx.IsCaptureLoaded())
    m_EmptyLabel->setText(tr("Open a capture to view bookmarks."));
  else
    m_EmptyLabel->setText(
        tr("No bookmarks yet.\nRight-click an event in the Event Browser to add one."));

  static_cast<QStackedLayout *>(layout())->setCurrentWidget(empty ? (QWidget *)m_EmptyLabel
                                                                  : (QWidget *)m_List);
  m_List->setEnabled(m_Ctx.IsCaptureLoaded());
  m_List->verticalScrollBar()->setValue(scrollValue);
  updateCurrentBookmark(m_Ctx.CurSelectedEvent());

  m_Refreshing = false;
}

void BookmarksView::showContextMenu(const QPoint &pos)
{
  QListWidgetItem *item = m_List->itemAt(pos);
  if(!item)
    return;

  if(!item->isSelected())
  {
    m_List->clearSelection();
    item->setSelected(true);
    m_List->setCurrentItem(item);
  }

  QMenu menu(this);
  QAction *rename = menu.addAction(Icons::page_white_edit(), tr("&Rename"));
  QAction *remove = menu.addAction(Icons::del(), tr("&Delete"));
  rename->setEnabled(m_List->selectedItems().count() == 1);

  QObject::connect(rename, &QAction::triggered, [this]() {
    if(m_List->selectedItems().count() == 1)
      renameBookmark(m_List->selectedItems()[0]->data(BookmarkEIDRole).toUInt());
  });
  QObject::connect(remove, &QAction::triggered, this, &BookmarksView::deleteSelectedBookmarks);

  RDDialog::show(&menu, m_List->viewport()->mapToGlobal(pos));
}

void BookmarksView::renameBookmark(uint32_t EID)
{
  EventBookmark editedBookmark;
  bool found = false;
  for(const EventBookmark &bookmark : m_Ctx.GetBookmarks())
  {
    if(bookmark.eventId == EID)
    {
      editedBookmark = bookmark;
      found = true;
      break;
    }
  }

  if(!found)
    return;

  bool ok = false;
  QString name = QInputDialog::getText(this, tr("Rename Bookmark"), tr("Name:"), QLineEdit::Normal,
                                       (QString)editedBookmark.text, &ok);
  if(ok)
  {
    editedBookmark.text = name.trimmed();
    m_Ctx.SetBookmark(editedBookmark);
  }
}

void BookmarksView::deleteBookmark(uint32_t EID)
{
  m_Ctx.RemoveBookmark(EID);
}

void BookmarksView::deleteSelectedBookmarks()
{
  QList<uint32_t> EIDs;
  for(QListWidgetItem *item : m_List->selectedItems())
    EIDs.push_back(item->data(BookmarkEIDRole).toUInt());
  deleteBookmarks(EIDs);
}

void BookmarksView::deleteBookmarks(const QList<uint32_t> &EIDs)
{
  rdcarray<uint32_t> bookmarks;
  bookmarks.reserve(EIDs.count());
  for(uint32_t EID : EIDs)
    bookmarks.push_back(EID);

  m_Ctx.RemoveBookmarks(bookmarks);
}

void BookmarksView::jumpToBookmark(uint32_t EID)
{
  EventBrowser *eventBrowser = static_cast<EventBrowser *>(m_Ctx.GetEventBrowser());
  eventBrowser->JumpToEID(EID);
}

void BookmarksView::saveBookmarkOrder()
{
  if(m_Refreshing)
    return;

  rdcarray<uint32_t> EIDs;
  EIDs.reserve(m_List->count());
  for(int row = 0; row < m_List->count(); ++row)
    EIDs.push_back(m_List->item(row)->data(BookmarkEIDRole).toUInt());

  m_Ctx.SetBookmarkOrder(EIDs);
}

void BookmarksView::updateCurrentBookmark(uint32_t EID)
{
  for(int row = 0; row < m_List->count(); ++row)
  {
    QListWidgetItem *item = m_List->item(row);
    QFont font = item->font();
    font.setBold(item->data(BookmarkEIDRole).toUInt() == EID);
    item->setFont(font);
  }
}
