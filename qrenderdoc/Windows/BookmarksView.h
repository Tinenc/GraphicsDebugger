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

#include <QCoreApplication>
#include <QFrame>
#include <QList>
#include "Code/Interface/QRDInterface.h"

class BookmarkListWidget;
class CaptureContext;
class QLabel;
class QListWidgetItem;

class BookmarksView : public QFrame, public ICaptureViewer
{
  Q_DECLARE_TR_FUNCTIONS(BookmarksView);

public:
  explicit BookmarksView(CaptureContext &ctx, QWidget *parent = 0);
  ~BookmarksView();

  void OnCaptureLoaded() override;
  void OnCaptureClosed() override;
  void OnSelectedEventChanged(uint32_t eventId) override;
  void OnEventChanged(uint32_t eventId) override;

  void BookmarksChanged();

private:
  void showContextMenu(const QPoint &pos);
  void renameBookmark(uint32_t EID);
  void deleteBookmark(uint32_t EID);
  void deleteSelectedBookmarks();
  void deleteBookmarks(const QList<uint32_t> &EIDs);
  void jumpToBookmark(uint32_t EID);
  void saveBookmarkOrder();
  void updateCurrentBookmark(uint32_t EID);

  CaptureContext &m_Ctx;
  BookmarkListWidget *m_List = NULL;
  QLabel *m_EmptyLabel = NULL;
  bool m_Refreshing = false;
  bool m_RefreshAfterDrag = false;
};
